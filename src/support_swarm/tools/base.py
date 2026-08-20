"""Tool execution: the single choke point for everything the swarm *does*.

No agent calls a provider or writes a row directly. Every action goes through
:meth:`ToolRunner.run`, which always performs the same six steps in the same
order:

1. resolve the tool spec (unknown tool => deny);
2. assemble policy facts from the database (account state, invoice, daily
   refund total) -- facts come from the system of record, never from the model;
3. derive a deterministic idempotency key and short-circuit on replay;
4. evaluate the guardrails;
5. execute, if and only if the decision is ALLOW;
6. write an ``action_log`` row and a ticket event either way.

Because step 6 happens on the deny and approval-required paths too, the audit
trail records refusals as first-class facts -- which is what you actually want
when someone asks "why didn't the bot refund this?".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..core.injection import InjectionVerdict
from ..core.logging import get_logger
from ..core.policy import (
    AccountFacts,
    ActionRequest,
    AgentRole,
    Decision,
    InvoiceFacts,
    Outcome,
    PolicyContext,
    PolicyLimits,
    Risk,
    ToolSpec,
    evaluate,
)
from ..db import repository as repo
from ..db.models import Ticket
from ..providers.base import ProviderBundle, ProviderResult

log = get_logger(__name__)


@dataclass
class ToolInvocation:
    """What a tool implementation receives."""

    session: AsyncSession
    providers: ProviderBundle
    ticket: Ticket
    params: dict[str, Any]
    idempotency_key: str
    settings: Settings
    account: AccountFacts | None = None
    invoice: InvoiceFacts | None = None


ToolFn = Callable[[ToolInvocation], Awaitable[ProviderResult]]


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    fn: ToolFn

    @property
    def name(self) -> str:
        return self.spec.name


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def spec(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return tool.spec if tool else None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def for_role(self, role: AgentRole) -> list[ToolSpec]:
        from ..core.policy import ROLE_SCOPES

        granted = ROLE_SCOPES.get(role, frozenset())
        return [t.spec for t in self._tools.values() if t.spec.scopes <= granted]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.spec.name,
                "risk": t.spec.risk.value,
                "scopes": sorted(s.value for s in t.spec.scopes),
                "description": t.spec.description,
                "requires_identity_verification": t.spec.requires_identity_verification,
            }
            for t in sorted(self._tools.values(), key=lambda x: x.name)
        ]


registry = ToolRegistry()


def tool(
    name: str,
    *,
    risk: Risk,
    scopes: set,
    description: str = "",
    idempotent: bool = True,
    requires_identity_verification: bool = False,
) -> Callable[[ToolFn], Tool]:
    """Decorator registering a coroutine as a policed tool."""

    def wrap(fn: ToolFn) -> Tool:
        doc_lines = (fn.__doc__ or "").strip().splitlines()
        spec = ToolSpec(
            name=name,
            risk=risk,
            scopes=frozenset(scopes),
            description=description or (doc_lines[0] if doc_lines else ""),
            idempotent=idempotent,
            requires_identity_verification=requires_identity_verification,
        )
        return registry.register(Tool(spec=spec, fn=fn))

    return wrap


def make_idempotency_key(ticket_id: str, tool_name: str, params: dict[str, Any]) -> str:
    """Deterministic key: the same logical action always hashes the same.

    Sorted-key JSON means parameter ordering cannot produce a second key for
    what is really one action, so a retried resolution replays rather than
    duplicates.
    """
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(f"{ticket_id}|{tool_name}|{canonical}".encode()).hexdigest()
    return f"{tool_name}:{digest[:40]}"


@dataclass
class ToolResult:
    """Outcome of one policed tool call."""

    tool: str
    status: str  # succeeded | failed | denied | approval_required | replayed
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    decision: Decision | None = None
    error: str | None = None
    action_id: str | None = None
    idempotency_key: str | None = None

    @property
    def denied(self) -> bool:
        return self.status == "denied"

    @property
    def needs_approval(self) -> bool:
        return self.status == "approval_required"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "decision": self.decision.as_dict() if self.decision else None,
            "idempotency_key": self.idempotency_key,
        }


class ToolRunner:
    """Executes tools on behalf of an agent, under policy."""

    def __init__(
        self,
        session: AsyncSession,
        providers: ProviderBundle,
        ticket: Ticket,
        *,
        injection: InjectionVerdict | None = None,
        settings: Settings | None = None,
        registry_override: ToolRegistry | None = None,
        now: datetime | None = None,
    ) -> None:
        self.session = session
        self.providers = providers
        self.ticket = ticket
        self.settings = settings or get_settings()
        self.registry = registry_override or registry
        self.injection = injection or InjectionVerdict(0)
        self._now = now
        #: Every call made in this run, for the escalation diagnostic bundle.
        self.trail: list[ToolResult] = []

    # -- helpers ---------------------------------------------------------- #

    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def limits(self) -> PolicyLimits:
        s = self.settings
        return PolicyLimits(
            refund_auto_approve_cents=s.refund_auto_approve_cents,
            refund_window_days=s.refund_window_days,
            refund_daily_total_cap_cents=s.refund_daily_total_cap_cents,
            allow_autonomous_financial_actions=s.allow_autonomous_financial_actions,
            block_actions_on_injection=s.block_actions_on_injection,
        )

    async def account_facts(self) -> AccountFacts | None:
        """Authoritative account state, including the identity-verification call.

        Identity is treated as verified when the ticket requester address matches
        the address on the customer record. Anything weaker (a name in the body,
        a claim in the body) is not identity, and a mismatch means high-risk
        tools are refused outright.
        """
        if not self.ticket.customer_id:
            return None
        customer = await repo.get_customer(self.session, self.ticket.customer_id)
        if customer is None:
            return None
        account = await repo.get_primary_account(self.session, customer.id)
        if account is None:
            return None
        verified = (
            self.ticket.requester_email.strip().lower() == customer.email.strip().lower()
            and not self.injection.hostile
        )
        return AccountFacts(
            account_id=account.id,
            customer_id=customer.id,
            status=account.status,
            plan=account.plan,
            region=account.region,
            identity_verified=verified,
        )

    async def invoice_facts(self, invoice_id: str | None) -> InvoiceFacts | None:
        if not invoice_id:
            return None
        invoice = await repo.get_invoice(self.session, invoice_id)
        if invoice is None:
            return None
        return InvoiceFacts(
            invoice_id=invoice.id,
            account_id=invoice.account_id,
            amount_cents=invoice.amount_cents,
            refunded_cents=invoice.refunded_cents,
            status=invoice.status,
            issued_at=invoice.issued_at,
        )

    # -- the one entry point ---------------------------------------------- #

    async def run(
        self,
        actor: AgentRole,
        tool_name: str,
        params: dict[str, Any] | None = None,
        *,
        approved_by: str | None = None,
    ) -> ToolResult:
        params = dict(params or {})
        tool_obj = self.registry.get(tool_name)
        spec = tool_obj.spec if tool_obj else None
        key = make_idempotency_key(self.ticket.id, tool_name, params)

        # Replay is checked BEFORE policy, deliberately. A completed action has
        # already changed the world -- a refund moved the refundable balance --
        # so re-evaluating the guardrails on a retry would deny it for a limit
        # its own first execution caused. That turns "your refund went through"
        # into "your refund was refused", which is the precise failure
        # idempotency exists to prevent. The key encodes ticket+tool+params, so
        # a hit means this exact logical action was already approved once.
        if spec is not None and spec.risk is not Risk.READ:
            replayed = await self._replay(tool_name, key, actor)
            if replayed is not None:
                return replayed

        account = await self.account_facts()
        invoice = await self.invoice_facts(params.get("invoice_id"))
        refunded_today = (
            await repo.refunded_today_cents(self.session, now=self.now())
            if spec and spec.risk is Risk.FINANCIAL
            else 0
        )

        ctx = PolicyContext(
            actor=actor,
            ticket_id=self.ticket.id,
            now=self.now(),
            limits=self.limits(),
            account=account,
            invoice=invoice,
            injection=self.injection,
            refunded_today_cents=refunded_today,
            human_approved_by=approved_by,
        )
        request = ActionRequest(tool=tool_name, params=params, idempotency_key=key)
        decision = evaluate(spec, request, ctx)

        # Reads are cheap and unlogged-as-actions; they still went through scope
        # checks above, and their results land in the ticket transcript.
        if spec is not None and spec.risk is Risk.READ and decision.allowed:
            return await self._execute_read(actor, tool_obj, params, key, account, invoice)

        if decision.outcome is Outcome.DENY:
            return await self._finish(
                actor, tool_name, spec, params, key, decision, "denied",
                ProviderResult(ok=False, error=";".join(decision.reasons)),
            )

        if decision.outcome is Outcome.APPROVAL_REQUIRED:
            return await self._finish(
                actor, tool_name, spec, params, key, decision, "approval_required",
                ProviderResult(ok=False, error="approval_required"),
            )

        assert tool_obj is not None and spec is not None  # ALLOW implies a real tool
        invocation = ToolInvocation(
            session=self.session,
            providers=self.providers,
            ticket=self.ticket,
            params=params,
            idempotency_key=key,
            settings=self.settings,
            account=account,
            invoice=invoice,
        )
        try:
            provider_result = await tool_obj.fn(invocation)
        except Exception as exc:  # a provider bug must not kill the resolution
            log.exception("tool raised", extra={"tool": tool_name})
            provider_result = ProviderResult(ok=False, error=f"tool_exception: {exc!s}")

        status = "succeeded" if provider_result.ok else "failed"
        return await self._finish(
            actor, tool_name, spec, params, key, decision, status, provider_result,
            approved_by=approved_by,
        )

    async def _replay(
        self, tool_name: str, key: str, actor: AgentRole
    ) -> ToolResult | None:
        """Return the original result if this exact action already succeeded."""
        previous = await repo.find_action_by_key(self.session, key)
        if previous is None or previous.status != "succeeded":
            return None

        log.info(
            "tool call replayed from idempotency key",
            extra={"tool": tool_name, "action_id": previous.id},
        )
        # Recorded in the transcript so a reader can tell a replay from a
        # second execution, without adding a second action_log row.
        await repo.append_event(
            self.session,
            ticket_id=self.ticket.id,
            actor=actor.value,
            kind="tool_call",
            payload={
                "tool": tool_name,
                "status": "replayed",
                "original_action_id": previous.id,
                "result": previous.result,
            },
        )
        result = ToolResult(
            tool=tool_name,
            status="replayed",
            ok=True,
            data=dict((previous.result or {}).get("data") or {}),
            action_id=previous.id,
            idempotency_key=key,
        )
        self.trail.append(result)
        return result

    async def _execute_read(
        self,
        actor: AgentRole,
        tool_obj: Tool | None,
        params: dict[str, Any],
        key: str,
        account: AccountFacts | None,
        invoice: InvoiceFacts | None,
    ) -> ToolResult:
        assert tool_obj is not None
        invocation = ToolInvocation(
            session=self.session,
            providers=self.providers,
            ticket=self.ticket,
            params=params,
            idempotency_key=key,
            settings=self.settings,
            account=account,
            invoice=invoice,
        )
        try:
            provider_result = await tool_obj.fn(invocation)
        except Exception as exc:
            log.exception("read tool raised", extra={"tool": tool_obj.name})
            provider_result = ProviderResult(ok=False, error=f"tool_exception: {exc!s}")

        await repo.append_event(
            self.session,
            ticket_id=self.ticket.id,
            actor=actor.value,
            kind="tool_call",
            payload={
                "tool": tool_obj.name,
                "params": params,
                "ok": provider_result.ok,
                "data": provider_result.data,
                "error": provider_result.error,
            },
        )
        result = ToolResult(
            tool=tool_obj.name,
            status="succeeded" if provider_result.ok else "failed",
            ok=provider_result.ok,
            data=provider_result.data,
            error=provider_result.error,
            idempotency_key=key,
        )
        self.trail.append(result)
        return result

    async def _finish(
        self,
        actor: AgentRole,
        tool_name: str,
        spec: ToolSpec | None,
        params: dict[str, Any],
        key: str,
        decision: Decision,
        status: str,
        provider_result: ProviderResult,
        *,
        approved_by: str | None = None,
    ) -> ToolResult:
        amount = params.get("amount_cents")
        action = await repo.record_action(
            self.session,
            ticket_id=self.ticket.id,
            # The clean key is reserved for the row that actually succeeded, so a
            # later retry can find it. Refusals and failures get a suffixed key:
            # they must be recorded without colliding on the unique index, and
            # without making a future attempt look like a completed one.
            idempotency_key=(
                key
                if status == "succeeded"
                else f"{key}:{status}:{self.now().timestamp()}"
            ),
            tool=tool_name,
            actor=actor.value,
            risk=spec.risk.value if spec else "unknown",
            status=status,
            request=params,
            result=provider_result.as_dict(),
            decision=decision.as_dict(),
            amount_cents=amount if isinstance(amount, int) and not isinstance(amount, bool) else 0,
            approved_by=approved_by,
        )
        await repo.append_event(
            self.session,
            ticket_id=self.ticket.id,
            actor=actor.value,
            kind="policy_decision" if status in {"denied", "approval_required"} else "tool_call",
            payload={
                "tool": tool_name,
                "status": status,
                "params": params,
                "decision": decision.as_dict(),
                "result": provider_result.as_dict(),
            },
        )
        log.info(
            "tool call finished",
            extra={
                "tool": tool_name,
                "actor": actor.value,
                "status": status,
                "outcome": decision.outcome.value,
                "reasons": decision.reasons,
            },
        )
        result = ToolResult(
            tool=tool_name,
            status=status,
            ok=provider_result.ok,
            data=provider_result.data,
            decision=decision,
            error=provider_result.error,
            action_id=action.id,
            idempotency_key=key,
        )
        self.trail.append(result)
        return result
