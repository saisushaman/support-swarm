"""The specialist worker agents.

Each one has a single job, a single privilege set, and no ability to do the
next one's work:

* :class:`DatabaseQueryAgent` -- read-only fact gathering.
* :class:`PermissionVerifierAgent` -- is this requester entitled to this?
* :class:`ApiActionAgent` -- turn an approved plan into tool calls.
* :class:`QualityAssuranceAgent` -- last gate before the customer sees anything;
  holds a veto.
* :class:`EscalationAgent` -- compile the human handoff.

The agents are deterministic by default. Where a model genuinely helps -- QA
judgement on reply quality -- the LLM is consulted and its answer is *narrowed*
by the deterministic checks, never trusted in place of them.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.injection import wrap_untrusted
from ..core.logging import get_logger
from ..core.policy import AgentRole
from ..db import repository as repo
from ..llm.gateway import LLMGateway
from ..llm.prompts import system_prompt
from ..tools.base import ToolResult
from .intents import Intent, plan_allows_feature
from .pipeline import QAVerdict, RunContext, Stage, Verification

log = get_logger(__name__)

_COMPROMISE = re.compile(
    r"\b(?:compromis(?:ed|e)|hacked|breached|stolen|someone else|unauthori[sz]ed)\b", re.I
)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# --------------------------------------------------------------------------- #
# 1. Database Query agent
# --------------------------------------------------------------------------- #


class DatabaseQueryAgent:
    """Gathers the facts a decision needs. Read-only, always."""

    role = AgentRole.DATABASE_QUERY

    async def gather(self, ctx: RunContext) -> dict[str, Any]:
        ctx.mark(Stage.GATHER)
        facts: dict[str, Any] = {}

        customer = await ctx.runner.run(
            self.role, "lookup_customer", {"email": ctx.ticket.requester_email}
        )
        facts["customer"] = customer.data if customer.ok else None
        facts["customer_found"] = customer.ok
        if not customer.ok:
            facts["lookup_error"] = customer.error
            ctx.facts = facts
            return facts

        account_block = customer.data.get("account") or {}
        account_id = account_block.get("account_id")
        facts["account_id"] = account_id

        if account_id:
            overview = await ctx.runner.run(
                self.role, "get_account_overview", {"account_id": account_id}
            )
            if overview.ok:
                facts["account"] = overview.data
                facts["entitlements"] = overview.data.get("entitlements", {})

            intent = ctx.classification.intent if ctx.classification else Intent.UNKNOWN
            if intent in (Intent.BILLING_REFUND, Intent.INVOICE_QUESTION):
                invoices = await ctx.runner.run(
                    self.role, "list_invoices", {"account_id": account_id, "limit": 10}
                )
                if invoices.ok:
                    facts["invoices"] = invoices.data.get("invoices", [])

        ctx.facts = facts
        ctx.state.facts = facts
        return facts

    async def resolve_target_invoice(self, ctx: RunContext) -> dict[str, Any] | None:
        """Pick the invoice a refund should apply to.

        A reference in the ticket body is honoured only if that invoice really
        belongs to this account -- otherwise a customer could name someone
        else's invoice id. Failing that, the most recent refundable invoice.
        """
        invoices: list[dict[str, Any]] = ctx.facts.get("invoices") or []
        if not invoices:
            return None
        reference = ctx.classification.invoice_reference if ctx.classification else None
        if reference:
            for inv in invoices:
                if inv.get("invoice_id") == reference:
                    return inv
        refundable = [
            i
            for i in invoices
            if i.get("status") == "paid" and int(i.get("refundable_cents") or 0) > 0
        ]
        return refundable[0] if refundable else None


# --------------------------------------------------------------------------- #
# 2. Permission Verifier agent
# --------------------------------------------------------------------------- #


class PermissionVerifierAgent:
    """Establishes whether the requester is entitled to what they asked for."""

    role = AgentRole.PERMISSION_VERIFIER

    async def verify(self, ctx: RunContext) -> Verification:
        ctx.mark(Stage.VERIFY)
        v = Verification()
        facts = ctx.facts
        intent = ctx.classification.intent if ctx.classification else Intent.UNKNOWN

        customer = facts.get("customer") or {}
        account = facts.get("account") or {}
        if not customer:
            v.blockers.append("customer_not_found")
            ctx.verification = v
            return v

        v.identity_verified = (
            str(customer.get("email", "")).strip().lower()
            == ctx.ticket.requester_email.strip().lower()
        )
        if not v.identity_verified:
            v.blockers.append("requester_email_does_not_match_account")

        status = str(account.get("status") or "unknown")
        v.evidence["account_status"] = status
        v.evidence["plan"] = account.get("plan")
        if status in {"fraud_hold", "closed"}:
            v.blockers.append(f"account_{status}")
        elif status == "suspended":
            v.blockers.append("account_suspended")

        if ctx.injection.hostile:
            v.blockers.append("hostile_injection_in_ticket")

        # intent-specific entitlement
        if intent in (Intent.PASSWORD_RESET, Intent.ACCOUNT_ACCESS):
            v.entitled = v.identity_verified and status not in {"fraud_hold", "closed"}
        elif intent == Intent.BILLING_REFUND:
            invoice = await DatabaseQueryAgent().resolve_target_invoice(ctx)
            if invoice is None:
                v.blockers.append("no_refundable_invoice_found")
                v.entitled = False
            else:
                v.evidence["target_invoice"] = invoice
                v.entitled = v.identity_verified
        elif intent == Intent.FEATURE_ACCESS:
            key = ctx.classification.feature_key if ctx.classification else None
            plan = str(account.get("plan") or "free")
            v.evidence["feature_key"] = key
            if not key:
                v.blockers.append("feature_not_identified")
                v.entitled = False
            elif not plan_allows_feature(plan, key):
                v.blockers.append("feature_requires_plan_upgrade")
                v.evidence["plan_allows"] = False
                v.entitled = False
            else:
                v.entitled = v.identity_verified
        elif intent == Intent.INVOICE_QUESTION:
            v.entitled = v.identity_verified
        else:
            v.blockers.append("intent_not_autonomously_resolvable")
            v.entitled = False

        await repo.append_event(
            ctx.runner.session,
            ticket_id=ctx.ticket.id,
            actor=self.role.value,
            kind="agent_thought",
            payload={"verification": v.as_dict(), "intent": intent.value},
        )
        ctx.verification = v
        return v


# --------------------------------------------------------------------------- #
# 3. API Action agent
# --------------------------------------------------------------------------- #


class ApiActionAgent:
    """Executes the plan. Every call goes through the policed ToolRunner."""

    role = AgentRole.API_ACTION

    async def act(self, ctx: RunContext, *, approved_by: str | None = None) -> list[ToolResult]:
        ctx.mark(Stage.ACT)
        intent = ctx.classification.intent if ctx.classification else Intent.UNKNOWN
        results: list[ToolResult] = []

        if intent in (Intent.PASSWORD_RESET, Intent.ACCOUNT_ACCESS):
            if intent is Intent.ACCOUNT_ACCESS and _COMPROMISE.search(
                f"{ctx.ticket.subject}\n{ctx.ticket.body}"
            ):
                results.append(
                    await ctx.runner.run(
                        self.role, "revoke_sessions", {}, approved_by=approved_by
                    )
                )
            results.append(
                await ctx.runner.run(self.role, "reset_password", {}, approved_by=approved_by)
            )

        elif intent is Intent.BILLING_REFUND:
            invoice = ctx.verification.evidence.get("target_invoice")
            if not invoice:
                return results
            requested = (
                ctx.classification.requested_amount_cents if ctx.classification else None
            )
            refundable = int(invoice.get("refundable_cents") or 0)
            # Never refund more than is actually refundable, whatever was asked.
            amount = min(requested, refundable) if requested else refundable
            results.append(
                await ctx.runner.run(
                    self.role,
                    "issue_refund",
                    {
                        "invoice_id": invoice.get("invoice_id"),
                        "amount_cents": amount,
                        "reason": "customer_request",
                    },
                    approved_by=approved_by,
                )
            )

        elif intent is Intent.FEATURE_ACCESS:
            key = ctx.classification.feature_key if ctx.classification else None
            if key:
                results.append(
                    await ctx.runner.run(
                        self.role,
                        "set_entitlement",
                        {"key": key, "enabled": True},
                        approved_by=approved_by,
                    )
                )

        # INVOICE_QUESTION needs no mutation -- the answer is the reply itself.
        ctx.actions.extend(results)
        return results

    async def send_reply(self, ctx: RunContext, body: str) -> ToolResult:
        ctx.mark(Stage.RESPOND)
        result = await ctx.runner.run(
            self.role,
            "send_customer_reply",
            {"subject": f"Re: {ctx.ticket.subject}", "body": body},
        )
        ctx.actions.append(result)
        return result


# --------------------------------------------------------------------------- #
# reply composition
# --------------------------------------------------------------------------- #


def compose_reply(ctx: RunContext) -> str:
    """Draft the customer-facing reply from what actually happened."""
    intent = ctx.classification.intent if ctx.classification else Intent.UNKNOWN
    name = (ctx.facts.get("customer") or {}).get("full_name") or "there"
    greeting = f"Hi {name.split()[0] if name != 'there' else 'there'},"
    succeeded = {a.tool for a in ctx.actions if a.ok}

    if intent in (Intent.PASSWORD_RESET, Intent.ACCOUNT_ACCESS) and "reset_password" in succeeded:
        lines = [
            greeting,
            "",
            "I've sent a password reset link to the email address on your account. "
            "It's valid for one hour. Open it and choose a new password to get back in.",
        ]
        if "revoke_sessions" in succeeded:
            lines.append(
                "\nBecause your message mentioned possible unauthorised access, I also "
                "signed out every device currently logged into the account. You'll need "
                "to sign in again after resetting."
            )
        lines += ["", "If the email hasn't arrived in a few minutes, check your spam folder "
                  "and reply here and I'll take another look.", "", "- Support"]
        return "\n".join(lines)

    if intent is Intent.BILLING_REFUND and "issue_refund" in succeeded:
        refund = next(a for a in ctx.actions if a.tool == "issue_refund" and a.ok)
        amount = int(refund.data.get("amount_cents") or 0)
        return "\n".join(
            [
                greeting,
                "",
                f"I've approved a refund of {_money(amount)} back to your original payment "
                "method. Most banks post it within 5-10 business days.",
                "",
                "You'll get a separate confirmation from our billing system with the "
                "reference number.",
                "",
                "- Support",
            ]
        )

    if intent is Intent.FEATURE_ACCESS and "set_entitlement" in succeeded:
        key = (
            ctx.classification.feature_key if ctx.classification else None
        ) or "the feature"
        pretty = key.replace("_", " ")
        return "\n".join(
            [
                greeting,
                "",
                f"{pretty.capitalize()} is now enabled on your account. Sign out and back "
                "in once, and it'll show up in your settings.",
                "",
                "- Support",
            ]
        )

    if intent is Intent.INVOICE_QUESTION:
        invoices = ctx.facts.get("invoices") or []
        if invoices:
            recent = invoices[0]
            body = [
                greeting,
                "",
                "Here's the breakdown of your most recent invoice:",
                "",
                f"- Amount: {_money(int(recent.get('amount_cents') or 0))}",
                f"- Status: {recent.get('status')}",
                f"- Issued: {(recent.get('issued_at') or '')[:10]}",
            ]
            if recent.get("description"):
                body.append(f"- For: {recent['description']}")
            if int(recent.get("refunded_cents") or 0) > 0:
                body.append(
                    f"- Already refunded: {_money(int(recent['refunded_cents']))}"
                )
            body += ["", "If something there doesn't look right, tell me which line and "
                     "I'll dig into it.", "", "- Support"]
            return "\n".join(body)

    return "\n".join(
        [
            greeting,
            "",
            "Thanks for getting in touch. I've passed this to a support engineer who can "
            "look at it properly - you'll hear back from them shortly.",
            "",
            "- Support",
        ]
    )


# --------------------------------------------------------------------------- #
# 4. Quality Assurance agent
# --------------------------------------------------------------------------- #

_INTERNAL_ID = re.compile(r"\b(?:cus|acct|inv|tkt|evt|act|esc|ent)_[A-Za-z0-9]{6,}\b")

#: Intents whose resolution is meaningless without a successful mutation.
_ACTION_REQUIRED: dict[Intent, tuple[str, ...]] = {
    Intent.PASSWORD_RESET: ("reset_password",),
    Intent.ACCOUNT_ACCESS: ("reset_password",),
    Intent.BILLING_REFUND: ("issue_refund",),
    Intent.FEATURE_ACCESS: ("set_entitlement",),
}


class QualityAssuranceAgent:
    """The veto. Deterministic checks first; the model may only add concerns."""

    role = AgentRole.QUALITY_ASSURANCE

    def __init__(self, llm: LLMGateway | None = None) -> None:
        self.llm = llm

    async def review(self, ctx: RunContext, draft: str) -> QAVerdict:
        ctx.mark(Stage.QA)
        intent = ctx.classification.intent if ctx.classification else Intent.UNKNOWN
        issues: list[str] = []
        verdict = "pass"
        revised: str | None = None

        # -- did the work actually happen? --------------------------------- #
        required = _ACTION_REQUIRED.get(intent, ())
        succeeded = {a.tool for a in ctx.actions if a.ok}
        if required and not set(required) & succeeded:
            issues.append(f"required_action_missing:{','.join(required)}")
            verdict = "escalate"

        if any(a.denied for a in ctx.actions):
            issues.append("action_denied_by_policy")
            verdict = "escalate"
        if any(a.needs_approval for a in ctx.actions):
            issues.append("action_awaiting_human_approval")
            verdict = "escalate"
        if any(a.status == "failed" for a in ctx.actions if a.tool != "send_customer_reply"):
            issues.append("action_failed_downstream")
            verdict = "escalate"

        # -- is the reply safe to send? ------------------------------------ #
        if not draft.strip():
            issues.append("empty_reply")
            verdict = "escalate"

        leaked = _INTERNAL_ID.findall(draft)
        if leaked:
            issues.append("internal_identifier_in_reply")
            revised = _INTERNAL_ID.sub("[redacted]", draft)
            if verdict == "pass":
                verdict = "revise"

        # A refund reply must state the amount that was really refunded.
        if intent is Intent.BILLING_REFUND and "issue_refund" in succeeded:
            actual = next(
                (int(a.data.get("amount_cents") or 0) for a in ctx.actions
                 if a.tool == "issue_refund" and a.ok),
                0,
            )
            if actual and _money(actual) not in draft:
                issues.append("refund_amount_not_stated_accurately")
                verdict = "escalate"

        if ctx.injection.suspicious:
            issues.append(f"injection_signals:{','.join(ctx.injection.signals)}")

        # -- optional model review: may tighten, never loosen -------------- #
        if self.llm is not None and verdict == "pass":
            model_verdict = await self._ask_model(ctx, revised or draft)
            if model_verdict and model_verdict.verdict in {"revise", "escalate"}:
                issues.extend(model_verdict.issues)
                verdict = model_verdict.verdict
                revised = model_verdict.revised_reply or revised

        result = QAVerdict(verdict=verdict, issues=issues, revised_reply=revised)
        await repo.append_event(
            ctx.runner.session,
            ticket_id=ctx.ticket.id,
            actor=self.role.value,
            kind="agent_thought",
            payload={"qa": result.as_dict(), "issues": issues},
        )
        ctx.qa = result
        return result

    async def _ask_model(self, ctx: RunContext, draft: str) -> QAVerdict | None:
        intent_label = ctx.classification.intent.value if ctx.classification else "unknown"
        try:
            response = await self.llm.complete(  # type: ignore[union-attr]
                [
                    {"role": "system", "content": system_prompt("quality_assurance")},
                    {
                        "role": "user",
                        "content": (
                            f"Intent: {intent_label}\n"
                            f"Actions taken: {[a.as_dict() for a in ctx.actions]}\n"
                            f"Verification: {ctx.verification.as_dict()}\n\n"
                            f"{wrap_untrusted(ctx.ticket.body, label='original_ticket')}\n\n"
                            f"Draft reply to review:\n---\n{draft}\n---"
                        ),
                    },
                ],
                role="qa",
            )
        except Exception as exc:
            # A model outage must not block a reply the deterministic checks
            # already cleared.
            log.warning("qa model review unavailable", extra={"error": str(exc)})
            return None

        payload = response.json(default={}) or {}
        raw = str(payload.get("verdict") or "pass").lower()
        if raw not in {"pass", "revise", "escalate"}:
            raw = "pass"
        issues = [str(i) for i in (payload.get("issues") or [])][:10]
        revised = payload.get("revised_reply")
        return QAVerdict(
            verdict=raw,
            issues=[f"model:{i}" for i in issues],
            revised_reply=str(revised) if revised else None,
        )


# --------------------------------------------------------------------------- #
# 5. Escalation agent
# --------------------------------------------------------------------------- #


class EscalationAgent:
    """Compiles the pre-digested handoff a human engineer actually wants."""

    role = AgentRole.ESCALATION

    async def escalate(
        self,
        ctx: RunContext,
        *,
        reason_code: str,
        severity: str = "normal",
    ) -> ToolResult:
        ctx.mark(Stage.ESCALATE)
        summary = self.build_summary(ctx, reason_code=reason_code)
        diagnostics = self.build_diagnostics(ctx, reason_code=reason_code)
        return await ctx.runner.run(
            self.role,
            "create_escalation",
            {
                "queue": ctx.runner.settings.escalation_queue,
                "severity": severity,
                "reason_code": reason_code,
                "summary_md": summary,
                "diagnostics": diagnostics,
            },
        )

    # -- summary construction ---------------------------------------------- #

    def build_summary(self, ctx: RunContext, *, reason_code: str) -> str:
        c = ctx.classification
        account = ctx.facts.get("account") or {}
        customer = ctx.facts.get("customer") or {}
        lines: list[str] = [
            f"# Escalation: {reason_code}",
            "",
            f"**Ticket** `{ctx.ticket.id}` - {ctx.ticket.subject or '(no subject)'}",
            f"**Channel** {ctx.ticket.channel} | **Priority** {ctx.ticket.priority}",
            "",
            "## What the customer asked",
            f"Classified as **{c.intent.value if c else 'unknown'}** "
            f"(confidence {c.confidence if c else 0.0}).",
        ]
        if c and c.requested_amount_cents:
            lines.append(f"Requested amount: **{_money(c.requested_amount_cents)}**.")
        if c and c.feature_key:
            lines.append(f"Requested feature: **{c.feature_key}**.")

        lines += [
            "",
            "## Account state",
            f"- Customer tier: `{customer.get('tier', 'unknown')}`",
            f"- Account status: `{account.get('status', 'unknown')}`",
            f"- Plan: `{account.get('plan', 'unknown')}` "
            f"({account.get('seats', '?')} seats, "
            f"MRR {_money(int(account.get('mrr_cents') or 0))})",
            f"- MFA enabled: `{account.get('mfa_enabled', 'unknown')}`",
        ]

        v = ctx.verification
        lines += [
            "",
            "## What was verified",
            f"- Identity verified: **{v.identity_verified}**",
            f"- Entitled to request: **{v.entitled}**",
        ]
        if v.blockers:
            lines.append("- Blockers: " + ", ".join(f"`{b}`" for b in v.blockers))

        invoice = v.evidence.get("target_invoice")
        if invoice:
            lines += [
                "",
                "## Target invoice",
                f"- Amount: {_money(int(invoice.get('amount_cents') or 0))}",
                f"- Already refunded: {_money(int(invoice.get('refunded_cents') or 0))}",
                f"- Refundable: {_money(int(invoice.get('refundable_cents') or 0))}",
                f"- Status: `{invoice.get('status')}` | "
                f"Issued: {(invoice.get('issued_at') or '')[:10]}",
            ]

        lines += ["", "## What the swarm attempted"]
        writes = [a for a in ctx.actions if a.tool != "send_customer_reply"]
        if not writes:
            lines.append("- No mutating action was attempted.")
        for action in writes:
            reasons = ", ".join(action.decision.reasons) if action.decision else ""
            lines.append(
                f"- `{action.tool}` -> **{action.status}**"
                + (f" ({reasons})" if reasons else "")
                + (f" - error: `{action.error}`" if action.error else "")
            )

        if ctx.injection.suspicious:
            lines += [
                "",
                "## :warning: Prompt-injection signals",
                f"Score **{ctx.injection.score}** - "
                + ", ".join(f"`{s}`" for s in ctx.injection.signals),
                "",
                "Autonomous authority was withdrawn for this ticket. Read the raw body "
                "before acting on anything it claims.",
            ]

        if ctx.qa.issues:
            lines += ["", "## QA findings"] + [f"- `{i}`" for i in ctx.qa.issues]

        lines += ["", "## Suggested next step", self._next_step(reason_code, ctx)]
        lines += [
            "",
            "## Pipeline stages reached",
            " -> ".join(ctx.stages) or "(none)",
        ]
        return "\n".join(lines)

    @staticmethod
    def _next_step(reason_code: str, ctx: RunContext) -> str:
        mapping = {
            "approval_required": (
                "Review the parked action above. If it is legitimate, approve it via "
                "`POST /v1/tickets/{id}/approve` - the swarm will execute and reply."
            ),
            "policy_denied": (
                "The action was refused outright, not merely held. Confirm the refusal "
                "reason is correct before overriding anything by hand."
            ),
            "identity_unverified": (
                "The requester's address does not match the account. Verify identity "
                "out of band before any credential or billing change."
            ),
            "hostile_injection": (
                "This ticket tried to manipulate the agent. Read the raw body, then "
                "decide whether the underlying request is even genuine."
            ),
            "feature_requires_plan_upgrade": (
                "This is an upsell decision, not a support fix. Route to the account "
                "owner rather than toggling the flag."
            ),
            "intent_not_autonomously_resolvable": (
                "Out of autonomous scope by design (subscription change or product bug). "
                "Handle manually."
            ),
            "customer_not_found": (
                "No account matches the requester address. Likely a wrong-address or "
                "spam ticket - confirm before replying."
            ),
            "qa_veto": (
                "QA blocked the drafted reply. Read the QA findings, correct the reply, "
                "and send manually."
            ),
            "action_failed": (
                "A downstream provider rejected the call. Check that provider's status "
                "before retrying."
            ),
        }
        return mapping.get(
            reason_code,
            "Review the transcript in `GET /v1/tickets/{id}` and continue manually.",
        )

    def build_diagnostics(self, ctx: RunContext, *, reason_code: str) -> dict[str, Any]:
        return {
            "reason_code": reason_code,
            "classification": ctx.classification.as_dict() if ctx.classification else None,
            "verification": ctx.verification.as_dict(),
            "facts": ctx.facts,
            "actions": [a.as_dict() for a in ctx.actions],
            "qa": ctx.qa.as_dict(),
            "injection": ctx.injection.as_dict(),
            "stages": list(ctx.stages),
        }
