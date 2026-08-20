"""Data access. The only module that writes SQL-ish code.

Kept as free functions taking an ``AsyncSession`` rather than a class, so tools
and the orchestrator can share one transaction per resolution run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.redaction import redact
from .models import (
    Account,
    ActionLog,
    Customer,
    Entitlement,
    Escalation,
    Invoice,
    Ticket,
    TicketEvent,
    utcnow,
)

# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


async def get_customer_by_email(session: AsyncSession, email: str) -> Customer | None:
    stmt = select(Customer).where(func.lower(Customer.email) == email.strip().lower())
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_customer(session: AsyncSession, customer_id: str) -> Customer | None:
    return await session.get(Customer, customer_id)


async def get_primary_account(session: AsyncSession, customer_id: str) -> Account | None:
    stmt = (
        select(Account)
        .where(Account.customer_id == customer_id)
        .order_by(Account.created_at.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_account(session: AsyncSession, account_id: str) -> Account | None:
    return await session.get(Account, account_id)


async def list_invoices(
    session: AsyncSession, account_id: str, *, limit: int = 20
) -> list[Invoice]:
    stmt = (
        select(Invoice)
        .where(Invoice.account_id == account_id)
        .order_by(Invoice.issued_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def get_invoice(session: AsyncSession, invoice_id: str) -> Invoice | None:
    return await session.get(Invoice, invoice_id)


async def latest_paid_invoice(session: AsyncSession, account_id: str) -> Invoice | None:
    stmt = (
        select(Invoice)
        .where(Invoice.account_id == account_id, Invoice.status == "paid")
        .order_by(Invoice.issued_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_entitlements(session: AsyncSession, account_id: str) -> list[Entitlement]:
    stmt = select(Entitlement).where(Entitlement.account_id == account_id)
    return list((await session.execute(stmt)).scalars())


async def get_entitlement(
    session: AsyncSession, account_id: str, key: str
) -> Entitlement | None:
    stmt = select(Entitlement).where(
        Entitlement.account_id == account_id, Entitlement.key == key
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def refunded_today_cents(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Total successfully refunded in the trailing 24h, for the daily cap."""
    since = (now or utcnow()) - timedelta(days=1)
    stmt = select(func.coalesce(func.sum(ActionLog.amount_cents), 0)).where(
        ActionLog.tool == "issue_refund",
        ActionLog.status == "succeeded",
        ActionLog.created_at >= since,
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


# --------------------------------------------------------------------------- #
# tickets & transcript
# --------------------------------------------------------------------------- #


async def create_ticket(
    session: AsyncSession,
    *,
    requester_email: str,
    subject: str,
    body: str,
    channel: str = "email",
    priority: str = "normal",
    customer_id: str | None = None,
    injection_score: int = 0,
) -> Ticket:
    ticket = Ticket(
        requester_email=requester_email,
        subject=subject,
        body=body,
        channel=channel,
        priority=priority,
        customer_id=customer_id,
        injection_score=injection_score,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: str) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def list_ticket_events(session: AsyncSession, ticket_id: str) -> list[TicketEvent]:
    stmt = (
        select(TicketEvent)
        .where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.seq.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def append_event(
    session: AsyncSession,
    *,
    ticket_id: str,
    actor: str,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> TicketEvent:
    """Append to the audit transcript. Payload is redacted before persisting."""
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(TicketEvent.seq), 0)).where(
                TicketEvent.ticket_id == ticket_id
            )
        )
    ).scalar_one() + 1
    event = TicketEvent(
        ticket_id=ticket_id,
        seq=next_seq,
        actor=actor,
        kind=kind,
        payload=redact(payload or {}),
    )
    session.add(event)
    await session.flush()
    return event


async def update_ticket(
    session: AsyncSession,
    ticket: Ticket,
    **fields: Any,
) -> Ticket:
    for key, value in fields.items():
        if not hasattr(ticket, key):
            raise AttributeError(f"Ticket has no field {key!r}")
        setattr(ticket, key, value)
    ticket.updated_at = utcnow()
    await session.flush()
    return ticket


# --------------------------------------------------------------------------- #
# action log (idempotency + audit)
# --------------------------------------------------------------------------- #


async def find_action_by_key(session: AsyncSession, idempotency_key: str) -> ActionLog | None:
    stmt = select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_action(
    session: AsyncSession,
    *,
    ticket_id: str | None,
    idempotency_key: str,
    tool: str,
    actor: str,
    risk: str,
    status: str,
    request: dict[str, Any],
    result: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    amount_cents: int = 0,
    approved_by: str | None = None,
) -> ActionLog:
    row = ActionLog(
        ticket_id=ticket_id,
        idempotency_key=idempotency_key,
        tool=tool,
        actor=actor,
        risk=risk,
        status=status,
        amount_cents=amount_cents,
        request=redact(request),
        result=redact(result or {}),
        decision=decision or {},
        approved_by=approved_by,
    )
    session.add(row)
    await session.flush()
    return row


async def finalise_action(
    session: AsyncSession,
    action: ActionLog,
    *,
    status: str,
    result: dict[str, Any] | None = None,
) -> ActionLog:
    action.status = status
    if result is not None:
        action.result = redact(result)
    await session.flush()
    return action


async def list_actions_for_ticket(session: AsyncSession, ticket_id: str) -> list[ActionLog]:
    stmt = (
        select(ActionLog)
        .where(ActionLog.ticket_id == ticket_id)
        .order_by(ActionLog.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars())


# --------------------------------------------------------------------------- #
# escalations
# --------------------------------------------------------------------------- #


async def create_escalation(
    session: AsyncSession,
    *,
    ticket_id: str,
    queue: str,
    severity: str,
    reason_code: str,
    summary_md: str,
    diagnostics: dict[str, Any] | None = None,
) -> Escalation:
    row = Escalation(
        ticket_id=ticket_id,
        queue=queue,
        severity=severity,
        reason_code=reason_code,
        summary_md=summary_md,
        diagnostics=redact(diagnostics or {}),
    )
    session.add(row)
    await session.flush()
    return row


async def list_escalations(
    session: AsyncSession, *, status: str | None = None, limit: int = 50
) -> list[Escalation]:
    stmt = select(Escalation).order_by(Escalation.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Escalation.status == status)
    return list((await session.execute(stmt)).scalars())


async def get_escalation_for_ticket(
    session: AsyncSession, ticket_id: str
) -> Escalation | None:
    stmt = (
        select(Escalation)
        .where(Escalation.ticket_id == ticket_id)
        .order_by(Escalation.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def now_utc() -> datetime:
    return datetime.now(UTC)
