"""The real HTTP provider path.

Everything else in the suite runs on stub providers, which means the code that
would talk to an actual billing or identity vendor could rot unnoticed. These
tests drive :mod:`support_swarm.providers.http` against a real ASGI service so
the wire behaviour is exercised: URL shape, auth header, idempotency header,
and — most importantly — that a rejected call is never retried.

Retrying a declined refund is how you issue two.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from support_swarm.providers.http import (
    HttpBillingProvider,
    HttpIdentityProvider,
    HttpNotificationProvider,
)


class Recorder:
    """Captures what the vendor actually received."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.attempts: dict[str, int] = {}


@pytest.fixture
def vendor():
    rec = Recorder()
    app = FastAPI()

    @app.post("/v1/refunds")
    async def refunds(request: Request, idempotency_key: str | None = Header(None)):
        body = await request.json()
        rec.calls.append(
            {
                "path": "/v1/refunds",
                "body": body,
                "idempotency_key": idempotency_key,
                "authorization": request.headers.get("authorization"),
            }
        )
        return {"refund_id": "re_1", "amount_cents": body["amount_cents"]}

    @app.post("/v1/refunds-declined")
    async def declined(request: Request):
        rec.calls.append({"path": "/v1/refunds-declined"})
        return JSONResponse({"error": "card_declined"}, status_code=402)

    @app.post("/v1/refunds-flaky")
    async def flaky(request: Request):
        rec.calls.append({"path": "/v1/refunds-flaky"})
        rec.attempts["flaky"] = rec.attempts.get("flaky", 0) + 1
        if rec.attempts["flaky"] < 3:
            return JSONResponse({"error": "upstream"}, status_code=503)
        return {"refund_id": "re_flaky", "amount_cents": 100}

    @app.post("/v1/password-resets")
    async def resets(request: Request, idempotency_key: str | None = Header(None)):
        body = await request.json()
        rec.calls.append({"path": "/v1/password-resets", "body": body,
                          "idempotency_key": idempotency_key})
        return {"reset_id": "rst_1", "replayed": False}

    @app.post("/v1/messages")
    async def messages(request: Request):
        rec.calls.append({"path": "/v1/messages", "body": await request.json()})
        return {"message_id": "msg_1", "idempotent_replay": True}

    rec.app = app
    return rec


def client_for(vendor) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=vendor.app),
        base_url="http://vendor",
        headers={"Authorization": "Bearer tok_123"},
    )


async def test_refund_sends_body_auth_and_idempotency_key(vendor):
    provider = HttpBillingProvider("http://vendor", client=client_for(vendor))
    result = await provider.issue_refund(
        account_id="acct_1",
        invoice_id="inv_1",
        amount_cents=2500,
        reason="customer_request",
        idempotency_key="issue_refund:abc123",
    )
    await provider.aclose()

    assert result.ok
    assert result.data["refund_id"] == "re_1"
    call = vendor.calls[0]
    assert call["body"] == {
        "account_id": "acct_1",
        "invoice_id": "inv_1",
        "amount_cents": 2500,
        "reason": "customer_request",
    }
    assert call["idempotency_key"] == "issue_refund:abc123"
    assert call["authorization"] == "Bearer tok_123"


async def test_a_declined_refund_is_never_retried(vendor):
    """The most important line in the provider layer."""
    provider = HttpBillingProvider("http://vendor", client=client_for(vendor))
    result = await provider._call(
        "POST", "/v1/refunds-declined", json={}, idempotency_key="k"
    )
    await provider.aclose()

    assert not result.ok
    assert result.error == "card_declined"
    assert len(vendor.calls) == 1


async def test_transient_5xx_is_retried_then_succeeds(vendor):
    provider = HttpBillingProvider("http://vendor", client=client_for(vendor), max_retries=2)
    result = await provider._call(
        "POST", "/v1/refunds-flaky", json={}, idempotency_key="k"
    )
    await provider.aclose()

    assert result.ok
    assert result.data["refund_id"] == "re_flaky"
    assert vendor.attempts["flaky"] == 3  # two failures, then the success


async def test_retries_are_bounded(vendor):
    provider = HttpBillingProvider("http://vendor", client=client_for(vendor), max_retries=0)
    result = await provider._call("POST", "/v1/refunds-flaky", json={}, idempotency_key="k")
    await provider.aclose()

    assert not result.ok
    assert vendor.attempts["flaky"] == 1


async def test_password_reset_posts_the_record_address(vendor):
    provider = HttpIdentityProvider("http://vendor", client=client_for(vendor))
    result = await provider.initiate_password_reset(
        customer_id="cus_1", email="ada@example.com", idempotency_key="k"
    )
    await provider.aclose()

    assert result.ok
    assert result.replayed is False
    assert vendor.calls[0]["body"] == {"customer_id": "cus_1", "email": "ada@example.com"}


async def test_vendor_reported_replay_is_surfaced(vendor):
    """So the tool layer does not double-count a refund the vendor deduplicated."""
    provider = HttpNotificationProvider("http://vendor", client=client_for(vendor))
    result = await provider.send(
        to="ada@example.com", subject="hi", body="hello", idempotency_key="k"
    )
    await provider.aclose()

    assert result.ok
    assert result.replayed is True


async def test_unreachable_host_returns_an_error_not_an_exception(vendor):
    """A vendor outage must degrade to a failed tool result, not crash a ticket."""
    provider = HttpBillingProvider(
        "http://127.0.0.1:9",  # reserved discard port
        timeout=0.25,
        max_retries=0,
    )
    result = await provider.issue_refund(
        account_id="a", invoice_id="i", amount_cents=1, reason="r", idempotency_key="k"
    )
    await provider.aclose()

    assert not result.ok
    assert result.error is not None
    assert "transport_error" in result.error
