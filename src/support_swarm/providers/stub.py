"""In-process provider stubs.

Deterministic, side-effect-visible implementations used by tests, CI and local
development. They record every call so a test can assert *exactly* what the
swarm would have done to the outside world, and they honour idempotency keys so
replay behaviour is exercised for real rather than assumed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .base import ProviderResult


def _token(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass
class CallRecord:
    provider: str
    operation: str
    payload: dict[str, Any]
    idempotency_key: str


@dataclass
class StubLedger:
    """Shared recorder so a test can inspect all providers at once."""

    calls: list[CallRecord] = field(default_factory=list)
    _seen: dict[str, ProviderResult] = field(default_factory=dict)
    #: Set an operation name here to force it to fail, e.g. {"issue_refund": "card_declined"}
    fail: dict[str, str] = field(default_factory=dict)

    def record(
        self, provider: str, operation: str, payload: dict[str, Any], key: str
    ) -> None:
        self.calls.append(CallRecord(provider, operation, dict(payload), key))

    def replay(self, key: str) -> ProviderResult | None:
        hit = self._seen.get(key)
        if hit is None:
            return None
        return ProviderResult(ok=hit.ok, data=dict(hit.data), error=hit.error, replayed=True)

    def remember(self, key: str, result: ProviderResult) -> ProviderResult:
        self._seen[key] = result
        return result

    def operations(self) -> list[str]:
        return [c.operation for c in self.calls]

    def find(self, operation: str) -> list[CallRecord]:
        return [c for c in self.calls if c.operation == operation]

    def reset(self) -> None:
        self.calls.clear()
        self._seen.clear()
        self.fail.clear()


class _StubBase:
    name = "stub"

    def __init__(self, ledger: StubLedger | None = None) -> None:
        self.ledger = ledger or StubLedger()

    def _dispatch(
        self, operation: str, payload: dict[str, Any], key: str, data: dict[str, Any]
    ) -> ProviderResult:
        replay = self.ledger.replay(key)
        if replay is not None:
            self.ledger.record(self.name, operation + ":replay", payload, key)
            return replay
        self.ledger.record(self.name, operation, payload, key)
        if operation in self.ledger.fail:
            return ProviderResult(ok=False, error=self.ledger.fail[operation])
        return self.ledger.remember(key, ProviderResult(ok=True, data=data))


class StubIdentityProvider(_StubBase):
    name = "identity"

    async def initiate_password_reset(
        self, *, customer_id: str, email: str, idempotency_key: str
    ) -> ProviderResult:
        return self._dispatch(
            "initiate_password_reset",
            {"customer_id": customer_id, "email": email},
            idempotency_key,
            {
                "reset_id": _token("reset", customer_id, idempotency_key),
                "delivery": "email",
                "expires_in_seconds": 3600,
            },
        )

    async def revoke_sessions(
        self, *, customer_id: str, idempotency_key: str
    ) -> ProviderResult:
        return self._dispatch(
            "revoke_sessions",
            {"customer_id": customer_id},
            idempotency_key,
            {"revoked": True},
        )


class StubBillingProvider(_StubBase):
    name = "billing"

    async def issue_refund(
        self,
        *,
        account_id: str,
        invoice_id: str,
        amount_cents: int,
        reason: str,
        idempotency_key: str,
    ) -> ProviderResult:
        return self._dispatch(
            "issue_refund",
            {
                "account_id": account_id,
                "invoice_id": invoice_id,
                "amount_cents": amount_cents,
                "reason": reason,
            },
            idempotency_key,
            {
                "refund_id": _token("refund", invoice_id, idempotency_key),
                "amount_cents": amount_cents,
                "invoice_id": invoice_id,
                "state": "succeeded",
            },
        )


class StubEntitlementProvider(_StubBase):
    name = "entitlement"

    async def set_flag(
        self, *, account_id: str, key: str, enabled: bool, idempotency_key: str
    ) -> ProviderResult:
        return self._dispatch(
            "set_flag",
            {"account_id": account_id, "key": key, "enabled": enabled},
            idempotency_key,
            {"account_id": account_id, "key": key, "enabled": enabled},
        )


class StubNotificationProvider(_StubBase):
    name = "notification"

    async def send(
        self, *, to: str, subject: str, body: str, idempotency_key: str
    ) -> ProviderResult:
        return self._dispatch(
            "send",
            {"to": to, "subject": subject, "body": body},
            idempotency_key,
            {"message_id": _token("msg", to, idempotency_key)},
        )


def build_stub_bundle(ledger: StubLedger | None = None):
    """Build a full bundle sharing one ledger."""
    from .base import ProviderBundle

    shared = ledger or StubLedger()
    return ProviderBundle(
        identity=StubIdentityProvider(shared),
        billing=StubBillingProvider(shared),
        entitlement=StubEntitlementProvider(shared),
        notification=StubNotificationProvider(shared),
    ), shared
