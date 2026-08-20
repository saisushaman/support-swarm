"""Real REST providers over httpx.

Shared behaviour lives in :class:`_HttpProvider`: bearer auth, an
``Idempotency-Key`` header on every mutation, bounded retries with exponential
backoff on transient failures only (timeouts, connection errors, 429, 5xx), and
no retry at all on 4xx -- retrying a rejected refund is how you get two.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import get_settings
from ..core.logging import get_logger
from ..core.redaction import redact
from .base import ProviderBundle, ProviderResult

log = get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class _HttpProvider:
    name = "http"

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 10.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "support-swarm/0.1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers
        )
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method, path, json=json, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt > self._max_retries:
                    log.warning(
                        "provider transport failure",
                        extra={"provider": self.name, "path": path, "error": str(exc)},
                    )
                    return ProviderResult(ok=False, error=f"transport_error: {exc!s}")
                await asyncio.sleep(0.2 * 2 ** (attempt - 1))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt <= self._max_retries:
                await asyncio.sleep(0.2 * 2 ** (attempt - 1))
                continue

            return self._to_result(response, path)

    def _to_result(self, response: httpx.Response, path: str) -> ProviderResult:
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:2000]}
        if not isinstance(body, dict):
            body = {"data": body}

        if response.is_success:
            return ProviderResult(
                ok=True,
                data=body,
                # Vendors that honour idempotency usually flag the replay.
                replayed=bool(body.get("replayed") or body.get("idempotent_replay")),
            )

        error = str(body.get("error") or body.get("message") or f"http_{response.status_code}")
        log.warning(
            "provider rejected call",
            extra={
                "provider": self.name,
                "path": path,
                "status": response.status_code,
                "body": redact(body),
            },
        )
        return ProviderResult(ok=False, data=body, error=error)


class HttpIdentityProvider(_HttpProvider):
    name = "identity"

    async def initiate_password_reset(
        self, *, customer_id: str, email: str, idempotency_key: str
    ) -> ProviderResult:
        return await self._call(
            "POST",
            "/v1/password-resets",
            json={"customer_id": customer_id, "email": email},
            idempotency_key=idempotency_key,
        )

    async def revoke_sessions(
        self, *, customer_id: str, idempotency_key: str
    ) -> ProviderResult:
        return await self._call(
            "POST",
            f"/v1/customers/{customer_id}/sessions:revoke",
            json={},
            idempotency_key=idempotency_key,
        )


class HttpBillingProvider(_HttpProvider):
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
        return await self._call(
            "POST",
            "/v1/refunds",
            json={
                "account_id": account_id,
                "invoice_id": invoice_id,
                "amount_cents": amount_cents,
                "reason": reason,
            },
            idempotency_key=idempotency_key,
        )


class HttpEntitlementProvider(_HttpProvider):
    name = "entitlement"

    async def set_flag(
        self, *, account_id: str, key: str, enabled: bool, idempotency_key: str
    ) -> ProviderResult:
        return await self._call(
            "PUT",
            f"/v1/accounts/{account_id}/entitlements/{key}",
            json={"enabled": enabled},
            idempotency_key=idempotency_key,
        )


class HttpNotificationProvider(_HttpProvider):
    name = "notification"

    async def send(
        self, *, to: str, subject: str, body: str, idempotency_key: str
    ) -> ProviderResult:
        return await self._call(
            "POST",
            "/v1/messages",
            json={"to": to, "subject": subject, "body": body},
            idempotency_key=idempotency_key,
        )


def build_http_bundle() -> ProviderBundle:
    s = get_settings()
    common = {
        "token": s.provider_token,
        "timeout": s.provider_timeout_seconds,
    }
    return ProviderBundle(
        identity=HttpIdentityProvider(s.identity_api_base, **common),
        billing=HttpBillingProvider(s.billing_api_base, **common),
        entitlement=HttpEntitlementProvider(s.entitlement_api_base, **common),
        notification=HttpNotificationProvider(s.notification_api_base, **common),
    )
