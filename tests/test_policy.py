"""Guardrail unit tests.

The policy engine is pure, so these are fast, exhaustive and the most valuable
tests in the suite: if one of them regresses, the swarm can move money it should
not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from support_swarm.core.injection import InjectionVerdict
from support_swarm.core.policy import (
    AccountFacts,
    ActionRequest,
    AgentRole,
    InvoiceFacts,
    Outcome,
    PolicyContext,
    PolicyLimits,
    Risk,
    Scope,
    ToolSpec,
    evaluate,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

REFUND = ToolSpec(
    name="issue_refund",
    risk=Risk.FINANCIAL,
    scopes=frozenset({Scope.BILLING_WRITE}),
)
RESET = ToolSpec(
    name="reset_password",
    risk=Risk.SENSITIVE_WRITE,
    scopes=frozenset({Scope.IDENTITY_WRITE}),
    requires_identity_verification=True,
)
FLAG = ToolSpec(
    name="set_entitlement",
    risk=Risk.LOW_WRITE,
    scopes=frozenset({Scope.ENTITLEMENT_WRITE}),
)
READ = ToolSpec(
    name="lookup_customer", risk=Risk.READ, scopes=frozenset({Scope.DB_READ})
)
ESCALATE = ToolSpec(
    name="create_escalation",
    risk=Risk.INTERNAL,
    scopes=frozenset({Scope.ESCALATION_CREATE}),
)


def account(**kw) -> AccountFacts:
    base = {
        "account_id": "acct_1",
        "customer_id": "cus_1",
        "status": "active",
        "plan": "pro",
        "identity_verified": True,
    }
    return AccountFacts(**{**base, **kw})


def invoice(**kw) -> InvoiceFacts:
    base = {
        "invoice_id": "inv_1",
        "account_id": "acct_1",
        "amount_cents": 9900,
        "refunded_cents": 0,
        "status": "paid",
        "issued_at": NOW - timedelta(days=5),
    }
    return InvoiceFacts(**{**base, **kw})


def ctx(**kw) -> PolicyContext:
    base = {
        "actor": AgentRole.API_ACTION,
        "ticket_id": "tkt_1",
        "now": NOW,
        "limits": PolicyLimits(),
        "account": account(),
        "invoice": invoice(),
    }
    return PolicyContext(**{**base, **kw})


def refund(amount: int = 5000, key: str = "k1") -> ActionRequest:
    return ActionRequest("issue_refund", {"amount_cents": amount}, idempotency_key=key)


# --- structure ------------------------------------------------------------- #


def test_unknown_tool_is_denied():
    decision = evaluate(None, ActionRequest("rm_rf", {}, idempotency_key="k"), ctx())
    assert decision.outcome is Outcome.DENY
    assert "unknown_tool" in decision.reasons


def test_write_without_idempotency_key_is_denied():
    request = ActionRequest("issue_refund", {"amount_cents": 100}, idempotency_key=None)
    decision = evaluate(REFUND, request, ctx())
    assert decision.outcome is Outcome.DENY
    assert "idempotency_key_required" in decision.reasons


def test_reads_do_not_require_an_idempotency_key():
    request = ActionRequest("lookup_customer", {}, idempotency_key=None)
    assert evaluate(READ, request, ctx(actor=AgentRole.DATABASE_QUERY)).allowed


# --- least privilege ------------------------------------------------------- #


@pytest.mark.parametrize(
    "role",
    [
        AgentRole.SUPERVISOR,
        AgentRole.DATABASE_QUERY,
        AgentRole.PERMISSION_VERIFIER,
        AgentRole.QUALITY_ASSURANCE,
        AgentRole.ESCALATION,
    ],
)
def test_only_the_api_action_agent_can_move_money(role):
    """The whole point of the scope model: nobody else gets billing:write."""
    decision = evaluate(REFUND, refund(), ctx(actor=role))
    assert decision.outcome is Outcome.DENY
    assert "scope_not_granted" in decision.reasons


def test_api_action_agent_cannot_create_escalations():
    """Otherwise it could route around QA by escalating its own failures."""
    request = ActionRequest("create_escalation", {}, idempotency_key="k")
    decision = evaluate(ESCALATE, request, ctx(actor=AgentRole.API_ACTION))
    assert decision.outcome is Outcome.DENY
    assert "scope_not_granted" in decision.reasons


# --- account state --------------------------------------------------------- #


@pytest.mark.parametrize("status", ["fraud_hold", "closed"])
def test_blocked_account_states_deny_writes_outright(status):
    decision = evaluate(REFUND, refund(), ctx(account=account(status=status)))
    assert decision.outcome is Outcome.DENY


def test_suspended_account_holds_for_a_human_rather_than_denying():
    decision = evaluate(REFUND, refund(), ctx(account=account(status="suspended")))
    assert decision.outcome is Outcome.APPROVAL_REQUIRED
    assert "account_suspended_requires_human_review" in decision.reasons


def test_missing_account_context_denies_writes():
    decision = evaluate(REFUND, refund(), ctx(account=None))
    assert decision.outcome is Outcome.DENY
    assert "account_context_missing" in decision.reasons


def test_credential_change_requires_verified_identity():
    request = ActionRequest("reset_password", {}, idempotency_key="k")
    decision = evaluate(RESET, request, ctx(account=account(identity_verified=False)))
    assert decision.outcome is Outcome.DENY
    assert "identity_not_verified" in decision.reasons


# --- money ----------------------------------------------------------------- #


def test_small_in_window_refund_is_allowed():
    assert evaluate(REFUND, refund(5000), ctx()).allowed


def test_refund_above_ceiling_needs_approval():
    decision = evaluate(REFUND, refund(20_001), ctx(invoice=invoice(amount_cents=50_000)))
    assert decision.outcome is Outcome.APPROVAL_REQUIRED
    assert "above_autonomous_refund_ceiling" in decision.reasons
    assert decision.obligations["autonomous_ceiling_cents"] == 20_000


def test_refund_exactly_at_ceiling_is_allowed():
    """Boundary: the ceiling is inclusive, so 20000 must not need approval."""
    decision = evaluate(REFUND, refund(20_000), ctx(invoice=invoice(amount_cents=50_000)))
    assert decision.allowed


def test_refund_beyond_refundable_balance_is_denied_not_held():
    decision = evaluate(
        REFUND, refund(9000), ctx(invoice=invoice(amount_cents=9900, refunded_cents=5000))
    )
    assert decision.outcome is Outcome.DENY
    assert "refund_exceeds_refundable_balance" in decision.reasons
    assert decision.obligations["refundable_cents"] == 4900


def test_outside_refund_window_needs_approval():
    old = invoice(issued_at=NOW - timedelta(days=200))
    decision = evaluate(REFUND, refund(1000), ctx(invoice=old))
    assert decision.outcome is Outcome.APPROVAL_REQUIRED
    assert "outside_refund_window" in decision.reasons
    assert decision.obligations["invoice_age_days"] == 200


def test_refund_window_boundary_is_inclusive():
    edge = invoice(issued_at=NOW - timedelta(days=90))
    assert evaluate(REFUND, refund(1000), ctx(invoice=edge)).allowed


def test_daily_cap_is_enforced_across_tickets():
    decision = evaluate(REFUND, refund(5000), ctx(refunded_today_cents=196_000))
    assert decision.outcome is Outcome.APPROVAL_REQUIRED
    assert "daily_refund_cap_exceeded" in decision.reasons


@pytest.mark.parametrize("status", ["open", "void", "uncollectible"])
def test_unpaid_invoices_are_not_refundable(status):
    decision = evaluate(REFUND, refund(1000), ctx(invoice=invoice(status=status)))
    assert decision.outcome is Outcome.DENY
    assert "invoice_not_refundable" in decision.reasons


def test_refund_against_another_accounts_invoice_is_denied():
    """A ticket naming someone else's invoice id must not reach billing."""
    decision = evaluate(REFUND, refund(1000), ctx(invoice=invoice(account_id="acct_other")))
    assert decision.outcome is Outcome.DENY
    assert "invoice_account_mismatch" in decision.reasons


@pytest.mark.parametrize("amount", [0, -100, None, "5000", 12.5, True])
def test_invalid_refund_amounts_are_denied(amount):
    request = ActionRequest("issue_refund", {"amount_cents": amount}, idempotency_key="k")
    assert evaluate(REFUND, request, ctx()).outcome is Outcome.DENY


def test_missing_invoice_is_denied():
    assert evaluate(REFUND, refund(), ctx(invoice=None)).outcome is Outcome.DENY


def test_financial_actions_can_be_globally_disabled():
    limits = PolicyLimits(allow_autonomous_financial_actions=False)
    decision = evaluate(REFUND, refund(100), ctx(limits=limits))
    assert decision.outcome is Outcome.APPROVAL_REQUIRED
    assert "autonomous_financial_actions_disabled" in decision.reasons


# --- prompt injection ------------------------------------------------------ #


def test_hostile_injection_blocks_all_writes():
    hostile = InjectionVerdict(score=9, signals=("override_instructions", "policy_bypass"))
    decision = evaluate(REFUND, refund(100), ctx(injection=hostile))
    assert decision.outcome is Outcome.DENY
    assert "hostile_injection_detected" in decision.reasons


def test_suspicious_injection_downgrades_high_risk_to_human_review():
    suspicious = InjectionVerdict(score=4, signals=("role_hijack",))
    decision = evaluate(REFUND, refund(100), ctx(injection=suspicious))
    assert decision.outcome is Outcome.APPROVAL_REQUIRED
    assert "suspicious_injection_requires_human_review" in decision.reasons


def test_suspicious_injection_does_not_block_low_risk_writes():
    suspicious = InjectionVerdict(score=4, signals=("role_hijack",))
    request = ActionRequest("set_entitlement", {"key": "dark_mode"}, idempotency_key="k")
    assert evaluate(FLAG, request, ctx(injection=suspicious)).allowed


def test_reads_survive_hostile_injection():
    """Gathering facts is how a human later understands the attack."""
    hostile = InjectionVerdict(score=12, signals=("exfiltration",))
    request = ActionRequest("lookup_customer", {}, idempotency_key=None)
    assert evaluate(
        READ, request, ctx(actor=AgentRole.DATABASE_QUERY, injection=hostile)
    ).allowed


def test_injection_block_can_be_relaxed_by_configuration():
    hostile = InjectionVerdict(score=9, signals=("override_instructions",))
    limits = PolicyLimits(block_actions_on_injection=False)
    decision = evaluate(REFUND, refund(100), ctx(injection=hostile, limits=limits))
    # Still not allowed outright -- it degrades to human review, not to yes.
    assert decision.outcome is Outcome.APPROVAL_REQUIRED


# --- the fail-safe path ---------------------------------------------------- #


@pytest.mark.parametrize("status", ["fraud_hold", "closed", "suspended"])
def test_escalation_is_never_blocked_by_account_state(status):
    """Regression: blocking escalation silently drops the tickets needing humans."""
    request = ActionRequest("create_escalation", {}, idempotency_key="k")
    decision = evaluate(
        ESCALATE, request, ctx(actor=AgentRole.ESCALATION, account=account(status=status))
    )
    assert decision.allowed


def test_escalation_is_never_blocked_by_hostile_injection():
    hostile = InjectionVerdict(score=15, signals=("override_instructions",))
    request = ActionRequest("create_escalation", {}, idempotency_key="k")
    decision = evaluate(
        ESCALATE, request, ctx(actor=AgentRole.ESCALATION, injection=hostile)
    )
    assert decision.allowed


def test_escalation_still_requires_an_idempotency_key():
    request = ActionRequest("create_escalation", {}, idempotency_key=None)
    decision = evaluate(ESCALATE, request, ctx(actor=AgentRole.ESCALATION))
    assert decision.outcome is Outcome.DENY


# --- human approval -------------------------------------------------------- #


def test_human_approval_converts_a_hold_into_an_allow():
    decision = evaluate(
        REFUND,
        refund(100_000),
        ctx(invoice=invoice(amount_cents=250_000), human_approved_by="oncall@example.com"),
    )
    assert decision.allowed
    assert any(r.startswith("human_approved:") for r in decision.reasons)


def test_human_approval_cannot_override_an_outright_denial():
    """Approval raises authority; it does not make an impossible refund possible."""
    decision = evaluate(
        REFUND,
        refund(50_000),
        ctx(invoice=invoice(amount_cents=9900), human_approved_by="oncall@example.com"),
    )
    assert decision.outcome is Outcome.DENY
    assert "refund_exceeds_refundable_balance" in decision.reasons


def test_human_approval_cannot_unblock_a_fraud_hold():
    decision = evaluate(
        REFUND,
        refund(100),
        ctx(account=account(status="fraud_hold"), human_approved_by="oncall@example.com"),
    )
    assert decision.outcome is Outcome.DENY
