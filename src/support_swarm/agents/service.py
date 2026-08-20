"""Service layer: the API's only door into the swarm.

Three operations:

* :func:`intake_ticket` -- persist a ticket, scan it for injection, link it to a
  customer record;
* :func:`resolve_ticket` -- run the swarm to a terminal state;
* :func:`approve_pending_action` -- a human authorises a parked action and the
  swarm finishes the job.

Approval deliberately *replays the whole pipeline* with an approval token rather
than firing the held call in isolation. Facts are re-read, guardrails re-run and
QA re-applied, so an approval granted at 09:00 cannot execute against 14:00
state that has since changed -- and because the idempotency key is derived from
the same inputs, the replay cannot double-charge.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core.errors import NotFoundError, ValidationError
from ..core.injection import scan
from ..core.logging import bind, get_logger, restore
from ..db import repository as repo
from ..db.base import session_scope
from ..db.models import Ticket
from ..llm.gateway import get_gateway
from ..providers import get_bundle
from ..session.store import get_store
from ..tools.base import ToolRunner
from .pipeline import Resolution, ResolutionStatus, RunContext

log = get_logger(__name__)


def build_orchestrator():
    """Pick the orchestrator named by configuration."""
    settings = get_settings()
    llm = get_gateway() if settings.llm_enabled else None
    if settings.orchestrator == "crewai":
        from .crew import CrewOrchestrator

        return CrewOrchestrator(llm)
    from .supervisor import DeterministicOrchestrator

    return DeterministicOrchestrator(llm)


async def intake_ticket(
    session: AsyncSession,
    *,
    requester_email: str,
    subject: str,
    body: str,
    channel: str = "email",
    priority: str = "normal",
) -> Ticket:
    """Persist an inbound ticket and record what we already know about it."""
    if not requester_email.strip():
        raise ValidationError("requester_email is required")
    if not (subject.strip() or body.strip()):
        raise ValidationError("ticket must have a subject or a body")

    verdict = scan(subject, body)
    customer = await repo.get_customer_by_email(session, requester_email)
    ticket = await repo.create_ticket(
        session,
        requester_email=requester_email,
        subject=subject,
        body=body,
        channel=channel,
        priority=priority,
        customer_id=customer.id if customer else None,
        injection_score=verdict.score,
    )
    await repo.append_event(
        session,
        ticket_id=ticket.id,
        actor="intake",
        kind="customer_message",
        payload={
            "subject": subject,
            "body": body,
            "channel": channel,
            "injection": verdict.as_dict(),
            "customer_matched": customer is not None,
        },
    )
    if verdict.suspicious:
        log.warning(
            "ticket contains injection signals",
            extra={"ticket_id": ticket.id, "signals": list(verdict.signals)},
        )
    return ticket


async def _build_context(session: AsyncSession, ticket: Ticket) -> RunContext:
    verdict = scan(ticket.subject, ticket.body)
    store = get_store()
    state = await store.load_or_create(ticket.id)
    state.injection = verdict.as_dict()
    providers = get_bundle()
    runner = ToolRunner(session, providers, ticket, injection=verdict)
    return RunContext(
        ticket=ticket,
        runner=runner,
        providers=providers,
        store=store,
        state=state,
        injection=verdict,
    )


async def _run(
    session: AsyncSession, ticket: Ticket, *, approved_by: str | None = None
) -> Resolution:
    settings = get_settings()
    orchestrator = build_orchestrator()
    ctx = await _build_context(session, ticket)
    previous = bind(ticket_id=ticket.id, orchestrator=orchestrator.name)
    try:
        return await asyncio.wait_for(
            orchestrator.resolve(ctx, approved_by=approved_by),
            timeout=settings.resolution_timeout_seconds,
        )
    except TimeoutError:
        log.error("resolution timed out", extra={"ticket_id": ticket.id})
        return await _fail_open(ctx, reason_code="resolution_timeout")
    except Exception as exc:
        log.exception("resolution crashed", extra={"ticket_id": ticket.id})
        return await _fail_open(ctx, reason_code="internal_error", detail=str(exc))
    finally:
        restore(previous)


async def _fail_open(
    ctx: RunContext, *, reason_code: str, detail: str | None = None
) -> Resolution:
    """A failed run must still leave a human holding the ticket, never silence.

    The escalation is written on a fresh transaction because the failure may
    have poisoned the one that was in flight.
    """
    ticket_id = ctx.ticket.id
    try:
        async with session_scope() as session:
            ticket = await repo.get_ticket(session, ticket_id)
            if ticket is None:
                raise NotFoundError(f"ticket {ticket_id} vanished mid-resolution")
            escalation = await repo.create_escalation(
                session,
                ticket_id=ticket_id,
                queue=get_settings().escalation_queue,
                severity="high",
                reason_code=reason_code,
                summary_md=(
                    f"# Escalation: {reason_code}\n\n"
                    f"The swarm could not complete this ticket automatically.\n\n"
                    f"**Ticket** `{ticket_id}`\n"
                    f"**Subject** {ticket.subject}\n"
                    f"**Stages reached** {' -> '.join(ctx.stages) or '(none)'}\n"
                    + (f"\n**Detail** `{detail}`\n" if detail else "")
                    + "\n## Suggested next step\n"
                    "Handle manually and check the swarm logs for this ticket id."
                ),
                diagnostics={"reason_code": reason_code, "detail": detail,
                             "stages": list(ctx.stages)},
            )
            await repo.update_ticket(
                session, ticket, status="failed", resolution=f"failed:{reason_code}"
            )
            escalation_id = escalation.id
    except Exception:
        log.exception("failed to record failure escalation", extra={"ticket_id": ticket_id})
        escalation_id = None

    return Resolution(
        ticket_id=ticket_id,
        status=ResolutionStatus.FAILED,
        intent=(ctx.classification.intent.value if ctx.classification else "unknown"),
        stages=list(ctx.stages),
        escalation_id=escalation_id,
        escalation_reason=reason_code,
        injection=ctx.injection.as_dict(),
    )


async def resolve_ticket(ticket_id: str) -> Resolution:
    """Run the swarm on an existing ticket, in its own transaction."""
    async with session_scope() as session:
        ticket = await repo.get_ticket(session, ticket_id)
        if ticket is None:
            raise NotFoundError(f"ticket {ticket_id} not found")
        return await _run(session, ticket)


async def intake_and_resolve(
    *,
    requester_email: str,
    subject: str,
    body: str,
    channel: str = "email",
    priority: str = "normal",
) -> tuple[str, Resolution]:
    """The common path: accept a ticket and resolve it in one call."""
    async with session_scope() as session:
        ticket = await intake_ticket(
            session,
            requester_email=requester_email,
            subject=subject,
            body=body,
            channel=channel,
            priority=priority,
        )
        resolution = await _run(session, ticket)
        return ticket.id, resolution


async def approve_pending_action(ticket_id: str, approver: str) -> Resolution:
    """A human authorises the parked action; the swarm finishes the job."""
    if not approver.strip():
        raise ValidationError("approver is required")
    async with session_scope() as session:
        ticket = await repo.get_ticket(session, ticket_id)
        if ticket is None:
            raise NotFoundError(f"ticket {ticket_id} not found")
        if ticket.status != "awaiting_approval":
            raise ValidationError(
                f"ticket {ticket_id} is {ticket.status}, not awaiting_approval"
            )
        state = await get_store().get(ticket_id)
        if state is None or not state.pending_action:
            raise ValidationError(
                f"ticket {ticket_id} has no parked action (session state expired)"
            )
        await repo.append_event(
            session,
            ticket_id=ticket_id,
            actor="human",
            kind="approval",
            payload={"approver": approver, "pending_action": state.pending_action},
        )
        return await _run(session, ticket, approved_by=approver)


async def add_followup(ticket_id: str, message: str) -> Resolution:
    """Append a customer follow-up and re-run resolution with it in context."""
    if not message.strip():
        raise ValidationError("message must not be empty")
    async with session_scope() as session:
        ticket = await repo.get_ticket(session, ticket_id)
        if ticket is None:
            raise NotFoundError(f"ticket {ticket_id} not found")

        combined = f"{ticket.body}\n\n--- follow-up ---\n{message}".strip()
        verdict = scan(ticket.subject, combined)
        await repo.update_ticket(
            session,
            ticket,
            body=combined,
            status="open",
            injection_score=verdict.score,
        )
        await repo.append_event(
            session,
            ticket_id=ticket_id,
            actor="customer",
            kind="customer_message",
            payload={"body": message, "injection": verdict.as_dict()},
        )
        return await _run(session, ticket)


async def ticket_transcript(ticket_id: str) -> dict[str, Any]:
    """Everything a human needs to audit one ticket."""
    async with session_scope() as session:
        ticket = await repo.get_ticket(session, ticket_id)
        if ticket is None:
            raise NotFoundError(f"ticket {ticket_id} not found")
        events = await repo.list_ticket_events(session, ticket_id)
        actions = await repo.list_actions_for_ticket(session, ticket_id)
        escalation = await repo.get_escalation_for_ticket(session, ticket_id)
        return {
            "ticket": {
                "id": ticket.id,
                "requester_email": ticket.requester_email,
                "subject": ticket.subject,
                "status": ticket.status,
                "intent": ticket.intent,
                "priority": ticket.priority,
                "channel": ticket.channel,
                "injection_score": ticket.injection_score,
                "resolution": ticket.resolution,
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
                "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
            },
            "events": [
                {
                    "seq": e.seq,
                    "actor": e.actor,
                    "kind": e.kind,
                    "payload": e.payload,
                    "at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in events
            ],
            "actions": [
                {
                    "tool": a.tool,
                    "actor": a.actor,
                    "risk": a.risk,
                    "status": a.status,
                    "amount_cents": a.amount_cents,
                    "decision": a.decision,
                    "approved_by": a.approved_by,
                    "at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in actions
            ],
            "escalation": None
            if escalation is None
            else {
                "id": escalation.id,
                "queue": escalation.queue,
                "severity": escalation.severity,
                "reason_code": escalation.reason_code,
                "status": escalation.status,
                "summary_md": escalation.summary_md,
            },
        }
