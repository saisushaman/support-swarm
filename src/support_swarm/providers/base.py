"""Provider contracts.

The swarm never calls a vendor SDK directly. Each capability is a narrow
protocol with two implementations -- :mod:`.stub` (deterministic, offline) and
:mod:`.http` (real REST) -- selected by ``SWARM_PROVIDER_MODE``. That keeps the
whole resolution pipeline testable without mocking at the HTTP layer, and means
a provider swap is a one-file change.

Every mutating call takes an ``idempotency_key``. Downstream systems are
expected to honour it; the swarm additionally records it in ``action_log`` so a
retried resolution cannot double-refund even if the vendor ignores the header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ProviderResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    #: True when the provider reported this call as a replay of an earlier one.
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "replayed": self.replayed,
        }


@runtime_checkable
class IdentityProvider(Protocol):
    async def initiate_password_reset(
        self, *, customer_id: str, email: str, idempotency_key: str
    ) -> ProviderResult: ...

    async def revoke_sessions(
        self, *, customer_id: str, idempotency_key: str
    ) -> ProviderResult: ...


@runtime_checkable
class BillingProvider(Protocol):
    async def issue_refund(
        self,
        *,
        account_id: str,
        invoice_id: str,
        amount_cents: int,
        reason: str,
        idempotency_key: str,
    ) -> ProviderResult: ...


@runtime_checkable
class EntitlementProvider(Protocol):
    async def set_flag(
        self,
        *,
        account_id: str,
        key: str,
        enabled: bool,
        idempotency_key: str,
    ) -> ProviderResult: ...


@runtime_checkable
class NotificationProvider(Protocol):
    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> ProviderResult: ...


@dataclass
class ProviderBundle:
    identity: IdentityProvider
    billing: BillingProvider
    entitlement: EntitlementProvider
    notification: NotificationProvider

    async def aclose(self) -> None:
        for provider in (self.identity, self.billing, self.entitlement, self.notification):
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()
