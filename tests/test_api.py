"""API contract: auth, scopes, validation and the happy path."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from support_swarm.api.main import create_app
from tests.conftest import API_KEY_ADMIN, API_KEY_AGENT

AGENT = {"X-API-Key": API_KEY_AGENT}
ADMIN = {"X-API-Key": API_KEY_ADMIN}


@pytest.fixture
async def client(accounts, ledger, store):
    """A client over the real ASGI app, sharing the test's DB and stubs."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


TICKET = {
    "requester_email": "ada@example.com",
    "subject": "Cannot log in",
    "body": "I forgot my password, please send a reset link.",
}


# --- auth ------------------------------------------------------------------ #


async def test_missing_api_key_is_rejected(client):
    response = await client.post("/v1/tickets", json=TICKET)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_invalid_api_key_is_rejected(client):
    response = await client.post("/v1/tickets", json=TICKET, headers={"X-API-Key": "nope"})
    assert response.status_code == 401


async def test_agent_scope_cannot_approve(client):
    """Approving a refund must need a stronger credential than filing a ticket."""
    response = await client.post(
        "/v1/tickets/tkt_whatever/approve", json={"approver": "me"}, headers=AGENT
    )
    assert response.status_code == 401
    assert "scope" in response.json()["message"]


async def test_agent_scope_cannot_read_the_escalation_queue(client):
    assert (await client.get("/v1/escalations", headers=AGENT)).status_code == 401


async def test_health_needs_no_credentials(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_reports_dependencies(client):
    body = (await client.get("/readyz")).json()
    assert body["database"] is True
    assert body["session_store"] is True


async def test_capabilities_exposes_tools_and_live_limits(client):
    body = (await client.get("/v1/capabilities")).json()
    names = {t["name"] for t in body["tools"]}
    assert {"issue_refund", "reset_password", "create_escalation"} <= names
    # Least privilege is visible, not just documented.
    assert "billing:write" not in body["role_scopes"]["database_query"]
    assert body["limits"]["refund_auto_approve_cents"] == 20000


# --- validation ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "payload",
    [
        {"requester_email": "not-an-email", "body": "help"},
        {"requester_email": "a@b.com", "body": ""},
        {"body": "missing email"},
        {"requester_email": "a@b.com", "body": "x", "channel": "carrier-pigeon"},
        {"requester_email": "a@b.com", "body": "x", "unexpected": 1},
    ],
)
async def test_malformed_tickets_are_rejected(client, payload):
    assert (await client.post("/v1/tickets", json=payload, headers=AGENT)).status_code == 422


async def test_unknown_ticket_returns_404(client):
    response = await client.get("/v1/tickets/tkt_missing", headers=AGENT)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --- happy path ------------------------------------------------------------ #


async def test_create_ticket_resolves_synchronously(client, ledger):
    response = await client.post("/v1/tickets", json=TICKET, headers=AGENT)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "resolved"
    assert body["intent"] == "password_reset"
    assert body["stages"][0] == "intake"
    assert ledger.find("initiate_password_reset")


async def test_background_mode_returns_a_ticket_id_immediately(client):
    response = await client.post(
        "/v1/tickets", json={**TICKET, "resolve_now": False}, headers=AGENT
    )
    assert response.status_code == 201
    assert response.json()["ticket_id"].startswith("tkt_")


async def test_transcript_endpoint_returns_the_audit_trail(client):
    created = await client.post("/v1/tickets", json=TICKET, headers=AGENT)
    ticket_id = created.json()["ticket_id"]

    body = (await client.get(f"/v1/tickets/{ticket_id}", headers=AGENT)).json()
    assert body["ticket"]["status"] == "resolved"
    assert len(body["events"]) > 3
    assert any(a["tool"] == "reset_password" for a in body["actions"])


async def test_request_id_is_echoed_for_correlation(client):
    response = await client.get("/healthz", headers={"X-Request-Id": "req-abc"})
    assert response.headers["X-Request-Id"] == "req-abc"


# --- approval flow over HTTP ----------------------------------------------- #


async def test_full_hold_then_approve_flow(client, ledger):
    created = await client.post(
        "/v1/tickets",
        json={
            "requester_email": "grace@example.com",
            "subject": "Billing error",
            "body": "We were double charged $2,500.00. Please refund the charge.",
        },
        headers=AGENT,
    )
    body = created.json()
    assert body["status"] == "awaiting_approval"
    ticket_id = body["ticket_id"]
    assert ledger.find("issue_refund") == []

    listed = (await client.get("/v1/escalations", headers=ADMIN)).json()
    assert any(e["ticket_id"] == ticket_id for e in listed)
    assert any(e["reason_code"] == "approval_required" for e in listed)

    approved = await client.post(
        f"/v1/tickets/{ticket_id}/approve",
        json={"approver": "oncall@example.com"},
        headers=ADMIN,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "resolved"
    assert len(ledger.find("issue_refund")) == 1


async def test_followup_message_endpoint(client):
    created = await client.post("/v1/tickets", json=TICKET, headers=AGENT)
    ticket_id = created.json()["ticket_id"]

    response = await client.post(
        f"/v1/tickets/{ticket_id}/messages",
        json={"message": "Any update?"},
        headers=AGENT,
    )
    assert response.status_code == 200
    assert response.json()["ticket_id"] == ticket_id


# --- api key parsing ------------------------------------------------------- #


def test_api_key_parsing_rejects_malformed_entries(monkeypatch):
    """A typo in configuration must not mint a working credential."""
    from support_swarm.config import Settings

    settings = Settings(
        api_keys="good:agent|admin, admin, :agent, bad:, weird:superuser, ok:agent"
    )
    parsed = settings.api_key_scopes()

    assert parsed == {"good": {"agent", "admin"}, "ok": {"agent"}}
    # "admin" with no colon was previously accepted as an agent key.
    assert "admin" not in parsed
    assert "bad" not in parsed
    assert "weird" not in parsed


def test_default_api_keys_provide_both_privilege_levels(monkeypatch):
    from support_swarm.config import Settings

    # The autouse fixture sets SWARM_API_KEYS; drop it to see the real default.
    monkeypatch.delenv("SWARM_API_KEYS", raising=False)
    parsed = Settings(_env_file=None).api_key_scopes()
    assert parsed["dev-key"] == {"agent"}
    assert parsed["dev-admin-key"] == {"agent", "admin"}


# --- orchestrator selection ------------------------------------------------- #


def test_selecting_crewai_without_the_package_fails_loudly(monkeypatch):
    """A missing optional dependency must not masquerade as "escalates everything".

    Skipped where CrewAI is installed; it asserts the missing-package path,
    which is the one a misconfigured deployment actually hits.
    """
    from importlib.util import find_spec

    if find_spec("crewai") is not None:
        pytest.skip("crewai is installed")

    from support_swarm.agents.service import build_orchestrator
    from support_swarm.config import reset_settings_cache

    monkeypatch.setenv("SWARM_ORCHESTRATOR", "crewai")
    reset_settings_cache()
    try:
        with pytest.raises(RuntimeError, match="requires CrewAI"):
            build_orchestrator()
    finally:
        reset_settings_cache()
