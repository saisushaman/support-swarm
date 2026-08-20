"""The guardrail engine.

Every mutating action in the swarm passes through :func:`evaluate` before it
touches a downstream API. The engine is deliberately:

* **pure** -- no I/O, no clock reads beyond the ``now`` passed in, so it is
  exhaustively unit-testable;
* **deny by default** -- an unknown tool or a missing scope is a refusal, not a
  warning;
* **independent of the LLM** -- a model can *propose* a $5,000 refund; only this
  module decides whether one happens. Prompt injection therefore cannot buy
  authority the calling agent does not already hold.

Outcomes are three-valued: ALLOW, APPROVAL_REQUIRED (legitimate but beyond
autonomous authority, which becomes a human escalation) and DENY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .injection import InjectionVerdict


class Risk(StrEnum):
    READ = "read"
    #: Writes to our own queues and outbound notifications. These must never be
    #: blocked by customer-facing guardrails: escalating and telling the customer
    #: a human is looking are the *fail-safe* path, so gating them on account
    #: state or injection score would silently drop exactly the tickets that most
    #: need a human. Still scope-checked, still audited, still idempotent.
    INTERNAL = "internal"
    LOW_WRITE = "low_write"
    SENSITIVE_WRITE = "sensitive_write"
    FINANCIAL = "financial"


class Scope(StrEnum):
    DB_READ = "db:read"
    IDENTITY_WRITE = "identity:write"
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"
    ENTITLEMENT_WRITE = "entitlement:write"
    NOTIFY_SEND = "notify:send"
    ESCALATION_CREATE = "escalation:create"


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    DATABASE_QUERY = "database_query"
    PERMISSION_VERIFIER = "permission_verifier"
    API_ACTION = "api_action"
    QUALITY_ASSURANCE = "quality_assurance"
    ESCALATION = "escalation"


#: Least-privilege scope grants. The Database Query agent can never move money;
#: the API Action agent cannot invent an escalation to route around QA.
ROLE_SCOPES: dict[AgentRole, frozenset[Scope]] = {
    AgentRole.SUPERVISOR: frozenset({Scope.DB_READ}),
    AgentRole.DATABASE_QUERY: frozenset({Scope.DB_READ, Scope.BILLING_READ}),
    AgentRole.PERMISSION_VERIFIER: frozenset({Scope.DB_READ, Scope.BILLING_READ}),
    AgentRole.API_ACTION: frozenset(
        {
            Scope.DB_READ,
            Scope.BILLING_READ,
            Scope.IDENTITY_WRITE,
            Scope.BILLING_WRITE,
            Scope.ENTITLEMENT_WRITE,
            Scope.NOTIFY_SEND,
        }
    ),
    AgentRole.QUALITY_ASSURANCE: frozenset({Scope.DB_READ, Scope.BILLING_READ}),
    AgentRole.ESCALATION: frozenset(
        {Scope.DB_READ, Scope.ESCALATION_CREATE, Scope.NOTIFY_SEND}
    ),
}


class Outcome(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


@dataclass(frozen=True)
class ToolSpec:
    """Static declaration of what a tool is and what it costs to call it."""

    name: str
    risk: Risk
    scopes: frozenset[Scope]
    description: str = ""
    idempotent: bool = True
    requires_identity_verification: bool = False


@dataclass
class AccountFacts:
    """The subset of account state the guardrails care about."""

    account_id: str
    customer_id: str
    status: str = "active"  # active | suspended | fraud_hold | closed
    plan: str = "free"
    region: str = "us"
    identity_verified: bool = False


@dataclass
class InvoiceFacts:
    invoice_id: str
    account_id: str
    amount_cents: int
    refunded_cents: int = 0
    status: str = "paid"  # paid | open | void | uncollectible
    issued_at: datetime | None = None

    @property
    def refundable_cents(self) -> int:
        return max(self.amount_cents - self.refunded_cents, 0)


@dataclass
class PolicyLimits:
    refund_auto_approve_cents: int = 20_000
    refund_window_days: int = 90
    refund_daily_total_cap_cents: int = 200_000
    allow_autonomous_financial_actions: bool = True
    block_actions_on_injection: bool = True


@dataclass
class PolicyContext:
    """Everything :func:`evaluate` is allowed to know."""

    actor: AgentRole
    ticket_id: str
    now: datetime
    limits: PolicyLimits = field(default_factory=PolicyLimits)
    account: AccountFacts | None = None
    invoice: InvoiceFacts | None = None
    injection: InjectionVerdict = field(default_factory=lambda: InjectionVerdict(0))
    refunded_today_cents: int = 0
    human_approved_by: str | None = None


@dataclass
class ActionRequest:
    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass
class Decision:
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    #: Machine-readable notes the caller must honour (e.g. the capped amount).
    obligations: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.outcome is Outcome.APPROVAL_REQUIRED

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "reasons": list(self.reasons),
            "obligations": dict(self.obligations),
        }


def _deny(reason: str, **obligations: Any) -> Decision:
    return Decision(Outcome.DENY, [reason], obligations)


def _hold(reasons: list[str], **obligations: Any) -> Decision:
    return Decision(Outcome.APPROVAL_REQUIRED, reasons, obligations)


def evaluate(spec: ToolSpec | None, request: ActionRequest, ctx: PolicyContext) -> Decision:
    """Decide whether ``request`` may execute. Never raises on policy grounds."""
    if spec is None:
        return _deny("unknown_tool", tool=request.tool)

    granted = ROLE_SCOPES.get(ctx.actor, frozenset())
    missing = sorted(s.value for s in spec.scopes - granted)
    if missing:
        return _deny("scope_not_granted", missing_scopes=missing, actor=ctx.actor.value)

    # --- internal actions: the fail-safe path stays open ------------------
    if spec.risk is Risk.INTERNAL:
        if not request.idempotency_key:
            return _deny("idempotency_key_required", tool=spec.name)
        return Decision(Outcome.ALLOW, ["internal_action"], {})

    is_write = spec.risk is not Risk.READ

    # --- prompt-injection containment -------------------------------------
    if is_write and ctx.injection.hostile and ctx.limits.block_actions_on_injection:
        return _deny("hostile_injection_detected", signals=list(ctx.injection.signals))

    holds: list[str] = []
    obligations: dict[str, Any] = {}

    if (
        ctx.injection.suspicious
        and spec.risk in (Risk.SENSITIVE_WRITE, Risk.FINANCIAL)
        and ctx.human_approved_by is None
    ):
        holds.append("suspicious_injection_requires_human_review")
        obligations["injection_signals"] = list(ctx.injection.signals)

    # --- account state ----------------------------------------------------
    if is_write:
        if ctx.account is None:
            return _deny("account_context_missing")
        if ctx.account.status == "fraud_hold":
            return _deny("account_on_fraud_hold", account_status=ctx.account.status)
        if ctx.account.status == "closed":
            return _deny("account_closed", account_status=ctx.account.status)
        if ctx.account.status == "suspended" and spec.risk is not Risk.LOW_WRITE:
            holds.append("account_suspended_requires_human_review")

    # --- structural requirements ------------------------------------------
    if is_write and not spec.idempotent:
        return _deny("non_idempotent_tool_not_permitted", tool=spec.name)
    if is_write and not request.idempotency_key:
        return _deny("idempotency_key_required", tool=spec.name)

    if spec.requires_identity_verification and not (
        ctx.account and ctx.account.identity_verified
    ):
        return _deny("identity_not_verified")

    # --- money -------------------------------------------------------------
    if spec.risk is Risk.FINANCIAL:
        money = _evaluate_refund(request, ctx)
        if money.outcome is Outcome.DENY:
            return money
        holds.extend(money.reasons)
        obligations.update(money.obligations)

    if holds:
        if ctx.human_approved_by:
            return Decision(
                Outcome.ALLOW,
                [f"human_approved:{ctx.human_approved_by}", *holds],
                obligations,
            )
        return _hold(holds, **obligations)

    return Decision(Outcome.ALLOW, ["policy_ok"], obligations)


def _evaluate_refund(request: ActionRequest, ctx: PolicyContext) -> Decision:
    """Financial-tier rules: DENY outright, or collect APPROVAL_REQUIRED reasons."""
    raw_amount = request.params.get("amount_cents")
    if not isinstance(raw_amount, int) or isinstance(raw_amount, bool):
        return _deny("refund_amount_invalid", amount_cents=raw_amount)
    if raw_amount <= 0:
        return _deny("refund_amount_must_be_positive", amount_cents=raw_amount)

    invoice = ctx.invoice
    if invoice is None:
        return _deny("invoice_not_found")
    if ctx.account and invoice.account_id != ctx.account.account_id:
        return _deny("invoice_account_mismatch")
    if invoice.status != "paid":
        return _deny("invoice_not_refundable", invoice_status=invoice.status)
    if raw_amount > invoice.refundable_cents:
        return _deny(
            "refund_exceeds_refundable_balance",
            requested_cents=raw_amount,
            refundable_cents=invoice.refundable_cents,
        )

    reasons: list[str] = []
    obligations: dict[str, Any] = {"amount_cents": raw_amount}

    if not ctx.limits.allow_autonomous_financial_actions:
        reasons.append("autonomous_financial_actions_disabled")

    if invoice.issued_at is not None:
        issued = invoice.issued_at
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=UTC)
        if ctx.now - issued > timedelta(days=ctx.limits.refund_window_days):
            reasons.append("outside_refund_window")
            obligations["invoice_age_days"] = (ctx.now - issued).days

    if raw_amount > ctx.limits.refund_auto_approve_cents:
        reasons.append("above_autonomous_refund_ceiling")
        obligations["autonomous_ceiling_cents"] = ctx.limits.refund_auto_approve_cents

    projected = ctx.refunded_today_cents + raw_amount
    if projected > ctx.limits.refund_daily_total_cap_cents:
        reasons.append("daily_refund_cap_exceeded")
        obligations["daily_cap_cents"] = ctx.limits.refund_daily_total_cap_cents
        obligations["refunded_today_cents"] = ctx.refunded_today_cents

    return Decision(
        Outcome.APPROVAL_REQUIRED if reasons else Outcome.ALLOW, reasons, obligations
    )
