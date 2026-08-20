"""The tool catalogue.

Read tools query the system of record. Write tools call a provider and then
reconcile local state so the database never claims a refund the billing system
did not make.

Importing this module is what populates
:data:`support_swarm.tools.base.registry`.
"""

from __future__ import annotations

from typing import Any

from ..core.policy import Risk, Scope
from ..db import repository as repo
from ..providers.base import ProviderResult
from .base import ToolInvocation, tool

# --------------------------------------------------------------------------- #
# read tools -- Database Query agent
# --------------------------------------------------------------------------- #


@tool(
    "lookup_customer",
    risk=Risk.READ,
    scopes={Scope.DB_READ},
    description="Find a customer and their primary account by email address.",
)
async def lookup_customer(inv: ToolInvocation) -> ProviderResult:
    """Find a customer and their primary account by email address."""
    email = str(inv.params.get("email") or inv.ticket.requester_email)
    customer = await repo.get_customer_by_email(inv.session, email)
    if customer is None:
        return ProviderResult(ok=False, error="customer_not_found", data={"email": email})
    account = await repo.get_primary_account(inv.session, customer.id)
    return ProviderResult(
        ok=True,
        data={
            "customer_id": customer.id,
            "email": customer.email,
            "full_name": customer.full_name,
            "tier": customer.tier,
            "account": None
            if account is None
            else {
                "account_id": account.id,
                "status": account.status,
                "plan": account.plan,
                "seats": account.seats,
                "mrr_cents": account.mrr_cents,
                "region": account.region,
                "mfa_enabled": account.mfa_enabled,
            },
        },
    )


@tool(
    "get_account_overview",
    risk=Risk.READ,
    scopes={Scope.DB_READ},
    description="Full account snapshot: status, plan, entitlements, recent invoices.",
)
async def get_account_overview(inv: ToolInvocation) -> ProviderResult:
    """Full account snapshot: status, plan, entitlements, recent invoices."""
    account_id = inv.params.get("account_id") or (inv.account.account_id if inv.account else None)
    if not account_id:
        return ProviderResult(ok=False, error="account_id_required")
    account = await repo.get_account(inv.session, str(account_id))
    if account is None:
        return ProviderResult(ok=False, error="account_not_found")
    entitlements = await repo.list_entitlements(inv.session, account.id)
    invoices = await repo.list_invoices(inv.session, account.id, limit=5)
    return ProviderResult(
        ok=True,
        data={
            "account_id": account.id,
            "customer_id": account.customer_id,
            "status": account.status,
            "plan": account.plan,
            "seats": account.seats,
            "mrr_cents": account.mrr_cents,
            "mfa_enabled": account.mfa_enabled,
            "entitlements": {e.key: e.enabled for e in entitlements},
            "recent_invoices": [
                {
                    "invoice_id": i.id,
                    "amount_cents": i.amount_cents,
                    "refunded_cents": i.refunded_cents,
                    "status": i.status,
                    "currency": i.currency,
                    "description": i.description,
                    "issued_at": i.issued_at.isoformat() if i.issued_at else None,
                }
                for i in invoices
            ],
        },
    )


@tool(
    "list_invoices",
    risk=Risk.READ,
    scopes={Scope.DB_READ, Scope.BILLING_READ},
    description="List recent invoices for an account, newest first.",
)
async def list_invoices(inv: ToolInvocation) -> ProviderResult:
    """List recent invoices for an account, newest first."""
    account_id = inv.params.get("account_id") or (inv.account.account_id if inv.account else None)
    if not account_id:
        return ProviderResult(ok=False, error="account_id_required")
    limit = int(inv.params.get("limit") or 10)
    invoices = await repo.list_invoices(inv.session, str(account_id), limit=limit)
    return ProviderResult(
        ok=True,
        data={
            "invoices": [
                {
                    "invoice_id": i.id,
                    "amount_cents": i.amount_cents,
                    "refunded_cents": i.refunded_cents,
                    "refundable_cents": max(i.amount_cents - i.refunded_cents, 0),
                    "status": i.status,
                    "currency": i.currency,
                    "description": i.description,
                    "issued_at": i.issued_at.isoformat() if i.issued_at else None,
                }
                for i in invoices
            ]
        },
    )


@tool(
    "get_invoice",
    risk=Risk.READ,
    scopes={Scope.DB_READ, Scope.BILLING_READ},
    description="Fetch one invoice, including how much of it is still refundable.",
)
async def get_invoice(inv: ToolInvocation) -> ProviderResult:
    """Fetch one invoice, including how much of it is still refundable."""
    invoice_id = inv.params.get("invoice_id")
    if not invoice_id:
        return ProviderResult(ok=False, error="invoice_id_required")
    invoice = await repo.get_invoice(inv.session, str(invoice_id))
    if invoice is None:
        return ProviderResult(ok=False, error="invoice_not_found")
    return ProviderResult(
        ok=True,
        data={
            "invoice_id": invoice.id,
            "account_id": invoice.account_id,
            "amount_cents": invoice.amount_cents,
            "refunded_cents": invoice.refunded_cents,
            "refundable_cents": max(invoice.amount_cents - invoice.refunded_cents, 0),
            "status": invoice.status,
            "currency": invoice.currency,
            "description": invoice.description,
            "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        },
    )


@tool(
    "list_entitlements",
    risk=Risk.READ,
    scopes={Scope.DB_READ},
    description="List feature entitlements for an account.",
)
async def list_entitlements(inv: ToolInvocation) -> ProviderResult:
    """List feature entitlements for an account."""
    account_id = inv.params.get("account_id") or (inv.account.account_id if inv.account else None)
    if not account_id:
        return ProviderResult(ok=False, error="account_id_required")
    rows = await repo.list_entitlements(inv.session, str(account_id))
    return ProviderResult(ok=True, data={"entitlements": {r.key: r.enabled for r in rows}})


# --------------------------------------------------------------------------- #
# write tools -- API Action agent
# --------------------------------------------------------------------------- #


@tool(
    "reset_password",
    risk=Risk.SENSITIVE_WRITE,
    scopes={Scope.IDENTITY_WRITE},
    description="Send a password-reset link to the address on the customer record.",
    requires_identity_verification=True,
)
async def reset_password(inv: ToolInvocation) -> ProviderResult:
    """Send a password-reset link to the address on the customer record.

    The destination address is read from the customer record, never from the
    ticket body -- otherwise a ticket claiming to be someone else could redirect
    the reset link.
    """
    if inv.account is None:
        return ProviderResult(ok=False, error="account_context_missing")
    customer = await repo.get_customer(inv.session, inv.account.customer_id)
    if customer is None:
        return ProviderResult(ok=False, error="customer_not_found")
    return await inv.providers.identity.initiate_password_reset(
        customer_id=customer.id,
        email=customer.email,
        idempotency_key=inv.idempotency_key,
    )


@tool(
    "revoke_sessions",
    risk=Risk.SENSITIVE_WRITE,
    scopes={Scope.IDENTITY_WRITE},
    description="Sign the customer out of every device (used for suspected compromise).",
    requires_identity_verification=True,
)
async def revoke_sessions(inv: ToolInvocation) -> ProviderResult:
    """Sign the customer out of every device (used for suspected compromise)."""
    if inv.account is None:
        return ProviderResult(ok=False, error="account_context_missing")
    return await inv.providers.identity.revoke_sessions(
        customer_id=inv.account.customer_id,
        idempotency_key=inv.idempotency_key,
    )


@tool(
    "issue_refund",
    risk=Risk.FINANCIAL,
    scopes={Scope.BILLING_WRITE},
    description="Refund part or all of a paid invoice.",
)
async def issue_refund(inv: ToolInvocation) -> ProviderResult:
    """Refund part or all of a paid invoice.

    Local state is updated only after the billing provider confirms, and only
    by the amount it actually refunded -- so a partial success cannot leave the
    database claiming more was returned than really was.
    """
    if inv.invoice is None:
        return ProviderResult(ok=False, error="invoice_not_found")
    amount = inv.params.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return ProviderResult(ok=False, error="refund_amount_invalid")

    result = await inv.providers.billing.issue_refund(
        account_id=inv.invoice.account_id,
        invoice_id=inv.invoice.invoice_id,
        amount_cents=amount,
        reason=str(inv.params.get("reason") or "customer_request"),
        idempotency_key=inv.idempotency_key,
    )
    if result.ok and not result.replayed:
        invoice = await repo.get_invoice(inv.session, inv.invoice.invoice_id)
        if invoice is not None:
            settled = int(result.data.get("amount_cents") or amount)
            invoice.refunded_cents = min(
                invoice.refunded_cents + settled, invoice.amount_cents
            )
            await inv.session.flush()
    return result


@tool(
    "set_entitlement",
    risk=Risk.LOW_WRITE,
    scopes={Scope.ENTITLEMENT_WRITE},
    description="Enable or disable a feature flag on an account.",
)
async def set_entitlement(inv: ToolInvocation) -> ProviderResult:
    """Enable or disable a feature flag on an account."""
    if inv.account is None:
        return ProviderResult(ok=False, error="account_context_missing")
    key = inv.params.get("key")
    if not key:
        return ProviderResult(ok=False, error="key_required")
    enabled = bool(inv.params.get("enabled", True))

    result = await inv.providers.entitlement.set_flag(
        account_id=inv.account.account_id,
        key=str(key),
        enabled=enabled,
        idempotency_key=inv.idempotency_key,
    )
    if result.ok:
        row = await repo.get_entitlement(inv.session, inv.account.account_id, str(key))
        if row is None:
            from ..db.models import Entitlement

            inv.session.add(
                Entitlement(account_id=inv.account.account_id, key=str(key), enabled=enabled)
            )
        else:
            row.enabled = enabled
        await inv.session.flush()
    return result


@tool(
    "send_customer_reply",
    risk=Risk.INTERNAL,
    scopes={Scope.NOTIFY_SEND},
    description="Send the final reply to the customer.",
)
async def send_customer_reply(inv: ToolInvocation) -> ProviderResult:
    """Send the final reply to the customer."""
    body = str(inv.params.get("body") or "")
    if not body.strip():
        return ProviderResult(ok=False, error="empty_reply")
    subject = str(inv.params.get("subject") or f"Re: {inv.ticket.subject}")
    return await inv.providers.notification.send(
        to=inv.ticket.requester_email,
        subject=subject,
        body=body,
        idempotency_key=inv.idempotency_key,
    )


# --------------------------------------------------------------------------- #
# escalation -- Escalation agent
# --------------------------------------------------------------------------- #


@tool(
    "create_escalation",
    risk=Risk.INTERNAL,
    scopes={Scope.ESCALATION_CREATE},
    description="Hand the ticket to a human queue with a diagnostic summary.",
)
async def create_escalation(inv: ToolInvocation) -> ProviderResult:
    """Hand the ticket to a human queue with a diagnostic summary."""
    params: dict[str, Any] = inv.params
    escalation = await repo.create_escalation(
        inv.session,
        ticket_id=inv.ticket.id,
        queue=str(params.get("queue") or inv.settings.escalation_queue),
        severity=str(params.get("severity") or "normal"),
        reason_code=str(params.get("reason_code") or "unresolved"),
        summary_md=str(params.get("summary_md") or ""),
        diagnostics=params.get("diagnostics") or {},
    )
    return ProviderResult(
        ok=True,
        data={
            "escalation_id": escalation.id,
            "queue": escalation.queue,
            "severity": escalation.severity,
            "reason_code": escalation.reason_code,
        },
    )
