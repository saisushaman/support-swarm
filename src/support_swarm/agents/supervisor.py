"""Supervisor and the reference orchestrator.

The Supervisor is the manager in the hierarchy: it triages, plans, and delegates
to the workers. It never touches a tool itself -- its scope grant is read-only,
so even a fully hijacked Supervisor cannot move money.

:class:`DeterministicOrchestrator` is the reference implementation of the
pipeline. It runs with no model at all, which is why it is what CI exercises and
what the CrewAI path is measured against.
"""

from __future__ import annotations

from typing import Any

from ..core.injection import wrap_untrusted
from ..core.logging import get_logger
from ..core.policy import AgentRole
from ..db import repository as repo
from ..llm.gateway import LLMGateway
from ..llm.prompts import system_prompt
from .intents import Classification, Intent, classify, coerce_intent
from .pipeline import Resolution, ResolutionStatus, RunContext, Stage
from .workers import (
    ApiActionAgent,
    DatabaseQueryAgent,
    EscalationAgent,
    PermissionVerifierAgent,
    QualityAssuranceAgent,
    compose_reply,
)

log = get_logger(__name__)

#: Blocker -> escalation reason code. Anything unmapped becomes a generic
#: verification failure rather than being silently treated as fine.
_BLOCKER_REASONS: dict[str, str] = {
    "requester_email_does_not_match_account": "identity_unverified",
    "customer_not_found": "customer_not_found",
    "account_fraud_hold": "account_state_blocked",
    "account_closed": "account_state_blocked",
    "account_suspended": "account_state_blocked",
    "hostile_injection_in_ticket": "hostile_injection",
    "feature_requires_plan_upgrade": "feature_requires_plan_upgrade",
    "feature_not_identified": "feature_not_identified",
    "no_refundable_invoice_found": "no_refundable_invoice",
    "intent_not_autonomously_resolvable": "intent_not_autonomously_resolvable",
}

_HIGH_SEVERITY_REASONS = frozenset(
    {"hostile_injection", "approval_required", "account_state_blocked", "identity_unverified"}
)

_HOLDING_REPLY = (
    "Hi there,\n\n"
    "Thanks for getting in touch. I've reviewed your request and passed it to a "
    "support engineer for a closer look - they'll follow up here directly.\n\n"
    "- Support"
)


class SupervisorAgent:
    """Triage and planning. Read-only by construction."""

    role = AgentRole.SUPERVISOR

    def __init__(self, llm: LLMGateway | None = None) -> None:
        self.llm = llm

    async def triage(self, ctx: RunContext) -> Classification:
        ctx.mark(Stage.TRIAGE)
        baseline = classify(ctx.ticket.subject, ctx.ticket.body)

        if self.llm is not None:
            model = await self._ask_model(ctx)
            if model is not None:
                baseline = self._merge(baseline, model)

        await repo.append_event(
            ctx.runner.session,
            ticket_id=ctx.ticket.id,
            actor=self.role.value,
            kind="agent_thought",
            payload={"classification": baseline.as_dict()},
        )
        ctx.classification = baseline
        ctx.state.intent = baseline.intent.value
        return baseline

    async def _ask_model(self, ctx: RunContext) -> Classification | None:
        try:
            response = await self.llm.complete(  # type: ignore[union-attr]
                [
                    {"role": "system", "content": system_prompt("supervisor")},
                    {
                        "role": "user",
                        "content": (
                            f"Subject: {ctx.ticket.subject}\n\n"
                            f"{wrap_untrusted(ctx.ticket.body)}"
                        ),
                    },
                ],
                role="supervisor",
            )
        except Exception as exc:
            log.warning("supervisor triage model unavailable", extra={"error": str(exc)})
            return None

        payload = response.json(default={}) or {}
        if not isinstance(payload, dict):
            return None
        amount = payload.get("requested_amount_cents")
        return Classification(
            intent=coerce_intent(payload.get("intent")),
            confidence=float(payload.get("confidence") or 0.0),
            requested_amount_cents=amount if isinstance(amount, int) else None,
            invoice_reference=(
                str(payload["invoice_reference"]) if payload.get("invoice_reference") else None
            ),
            reasoning=str(payload.get("reasoning") or "")[:300],
        )

    @staticmethod
    def _merge(baseline: Classification, model: Classification) -> Classification:
        """Combine rule and model triage.

        The model wins on intent only when it is both confident and the rules
        found nothing; extraction fields fall back to whichever found a value.
        Anything the model says is still constrained to the taxonomy.
        """
        intent = baseline.intent
        rules_found_nothing = baseline.intent is Intent.UNKNOWN
        model_is_more_confident = model.confidence >= 0.85 > baseline.confidence
        if model.intent is not Intent.UNKNOWN and (
            rules_found_nothing or model_is_more_confident
        ):
            intent = model.intent

        return Classification(
            intent=intent,
            confidence=max(
                baseline.confidence,
                model.confidence if intent is model.intent else 0.0,
            ),
            scores=baseline.scores,
            requested_amount_cents=baseline.requested_amount_cents
            or model.requested_amount_cents,
            invoice_reference=baseline.invoice_reference or model.invoice_reference,
            feature_key=baseline.feature_key,
            reasoning=model.reasoning or baseline.reasoning,
        )


class DeterministicOrchestrator:
    """Runs the seven-stage pipeline with explicit control flow."""

    name = "deterministic"

    def __init__(self, llm: LLMGateway | None = None) -> None:
        self.supervisor = SupervisorAgent(llm)
        self.db_agent = DatabaseQueryAgent()
        self.verifier = PermissionVerifierAgent()
        self.api_agent = ApiActionAgent()
        self.qa_agent = QualityAssuranceAgent(llm)
        self.escalation_agent = EscalationAgent()

    # -- entry point -------------------------------------------------------- #

    async def resolve(
        self, ctx: RunContext, *, approved_by: str | None = None
    ) -> Resolution:
        ctx.mark(Stage.INTAKE)
        ctx.state.add_turn("customer", ctx.ticket.body)

        classification = await self.supervisor.triage(ctx)
        await repo.update_ticket(
            ctx.runner.session, ctx.ticket, intent=classification.intent.value
        )

        if not classification.autonomous:
            return await self.escalate_with(
                ctx, reason_code="intent_not_autonomously_resolvable"
            )

        facts = await self.db_agent.gather(ctx)
        if not facts.get("customer_found"):
            return await self.escalate_with(ctx, reason_code="customer_not_found")

        if ctx.injection.hostile:
            return await self.escalate_with(ctx, reason_code="hostile_injection")

        await self.verifier.verify(ctx)
        return await self.finish_from_verified(ctx, approved_by=approved_by)

    async def finish_from_verified(
        self, ctx: RunContext, *, approved_by: str | None = None
    ) -> Resolution:
        """Stages act -> QA -> respond.

        Split out from :meth:`resolve` so the CrewAI orchestrator reuses the
        exact same execution and finalisation path instead of reimplementing
        (and inevitably diverging from) the guarantees.
        """
        verification = ctx.verification
        if not verification.entitled:
            reason = next(
                (_BLOCKER_REASONS[b] for b in verification.blockers if b in _BLOCKER_REASONS),
                "verification_failed",
            )
            return await self.escalate_with(ctx, reason_code=reason)

        actions = await self.api_agent.act(ctx, approved_by=approved_by)

        if any(a.needs_approval for a in actions):
            return await self.park_for_approval(ctx, actions)
        if any(a.denied for a in actions):
            return await self.escalate_with(ctx, reason_code="policy_denied")
        if any(a.status == "failed" for a in actions):
            return await self.escalate_with(ctx, reason_code="action_failed")

        draft = compose_reply(ctx)
        ctx.draft_reply = draft
        qa = await self.qa_agent.review(ctx, draft)
        if qa.verdict == "escalate":
            return await self.escalate_with(ctx, reason_code="qa_veto")

        final_reply = qa.revised_reply or draft
        send = await self.api_agent.send_reply(ctx, final_reply)
        if not send.ok:
            return await self.escalate_with(ctx, reason_code="reply_delivery_failed")

        await repo.update_ticket(
            ctx.runner.session, ctx.ticket, status="resolved", resolution=final_reply
        )
        ctx.state.add_turn("agent", final_reply)
        await ctx.store.put(ctx.state)

        return Resolution(
            ticket_id=ctx.ticket.id,
            status=ResolutionStatus.RESOLVED,
            intent=(ctx.classification.intent.value if ctx.classification else "unknown"),
            reply=final_reply,
            stages=list(ctx.stages),
            actions=[a.as_dict() for a in ctx.actions],
            qa=qa.as_dict(),
            verification=ctx.verification.as_dict(),
            injection=ctx.injection.as_dict(),
        )

    # -- terminal paths ----------------------------------------------------- #

    async def escalate_with(self, ctx: RunContext, *, reason_code: str) -> Resolution:
        severity = "high" if reason_code in _HIGH_SEVERITY_REASONS else "normal"
        result = await self.escalation_agent.escalate(
            ctx, reason_code=reason_code, severity=severity
        )
        escalation_id = result.data.get("escalation_id") if result.ok else None

        # A templated holding note: no claims about outcomes, so it needs no QA
        # pass, but the customer is never left in silence.
        await self.api_agent.send_reply(ctx, _HOLDING_REPLY)

        await repo.update_ticket(
            ctx.runner.session,
            ctx.ticket,
            status="escalated",
            resolution=f"escalated:{reason_code}",
        )
        await ctx.store.put(ctx.state)

        return Resolution(
            ticket_id=ctx.ticket.id,
            status=ResolutionStatus.ESCALATED,
            intent=(ctx.classification.intent.value if ctx.classification else "unknown"),
            reply=_HOLDING_REPLY,
            stages=list(ctx.stages),
            actions=[a.as_dict() for a in ctx.actions],
            escalation_id=escalation_id,
            escalation_reason=reason_code,
            qa=ctx.qa.as_dict(),
            verification=ctx.verification.as_dict(),
            injection=ctx.injection.as_dict(),
        )

    async def park_for_approval(self, ctx: RunContext, actions: list[Any]) -> Resolution:
        """Hold the action, escalate for a human, and keep enough state to resume."""
        held = next(a for a in actions if a.needs_approval)
        pending = {
            "tool": held.tool,
            "params": self._held_params(ctx, held.tool),
            "reasons": held.decision.reasons if held.decision else [],
            "obligations": held.decision.obligations if held.decision else {},
        }
        ctx.state.pending_action = pending
        await ctx.store.put(ctx.state)

        result = await self.escalation_agent.escalate(
            ctx, reason_code="approval_required", severity="high"
        )
        await self.api_agent.send_reply(ctx, _HOLDING_REPLY)
        await repo.update_ticket(
            ctx.runner.session,
            ctx.ticket,
            status="awaiting_approval",
            resolution=f"awaiting_approval:{held.tool}",
        )

        return Resolution(
            ticket_id=ctx.ticket.id,
            status=ResolutionStatus.AWAITING_APPROVAL,
            intent=(ctx.classification.intent.value if ctx.classification else "unknown"),
            reply=_HOLDING_REPLY,
            stages=list(ctx.stages),
            actions=[a.as_dict() for a in ctx.actions],
            escalation_id=result.data.get("escalation_id") if result.ok else None,
            escalation_reason="approval_required",
            qa=ctx.qa.as_dict(),
            verification=ctx.verification.as_dict(),
            injection=ctx.injection.as_dict(),
            pending_action=pending,
        )

    @staticmethod
    def _held_params(ctx: RunContext, tool: str) -> dict[str, Any]:
        """Reconstruct the exact params of the held call so approval replays it."""
        for action in reversed(ctx.actions):
            if action.tool == tool and action.decision is not None:
                obligations = action.decision.obligations
                if tool == "issue_refund":
                    invoice = ctx.verification.evidence.get("target_invoice") or {}
                    return {
                        "invoice_id": invoice.get("invoice_id"),
                        "amount_cents": obligations.get("amount_cents"),
                        "reason": "customer_request",
                    }
                if tool == "set_entitlement":
                    return {
                        "key": ctx.classification.feature_key if ctx.classification else None,
                        "enabled": True,
                    }
                return {}
        return {}
