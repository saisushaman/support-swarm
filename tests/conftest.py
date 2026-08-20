"""Shared fixtures.

Every test runs against SQLite, the in-process session store and stub providers,
so the suite needs no daemon, no network and no model credentials -- and the
stub ledger lets a test assert exactly what the swarm *would* have done to the
outside world.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from support_swarm.config import get_settings, reset_settings_cache
from support_swarm.db.base import create_all, dispose, session_scope
from support_swarm.db.models import Account, Customer, Entitlement, Invoice, utcnow
from support_swarm.providers import set_bundle
from support_swarm.providers.stub import build_stub_bundle
from support_swarm.session.store import MemorySessionStore, set_store

API_KEY_AGENT = "test-agent-key"
API_KEY_ADMIN = "test-admin-key"


@pytest.fixture(autouse=True)
def settings_env(monkeypatch, tmp_path):
    """Point every setting at a hermetic, per-test target."""
    db_path = (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("SWARM_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("SWARM_SESSION_URL", "memory://")
    monkeypatch.setenv("SWARM_PROVIDER_MODE", "stub")
    monkeypatch.setenv("SWARM_ORCHESTRATOR", "deterministic")
    monkeypatch.setenv("SWARM_LLM_ENABLED", "false")
    monkeypatch.setenv("SWARM_LOG_JSON", "false")
    monkeypatch.setenv("SWARM_LOG_LEVEL", "CRITICAL")
    monkeypatch.setenv(
        "SWARM_API_KEYS", f"{API_KEY_AGENT}:agent,{API_KEY_ADMIN}:agent|admin"
    )
    monkeypatch.setenv("SWARM_REFUND_AUTO_APPROVE_CENTS", "20000")
    monkeypatch.setenv("SWARM_REFUND_DAILY_TOTAL_CAP_CENTS", "200000")
    monkeypatch.setenv("SWARM_REFUND_WINDOW_DAYS", "90")
    reset_settings_cache()
    yield get_settings()
    reset_settings_cache()


@pytest.fixture
async def db(settings_env):
    """Fresh schema bound to this test's database URL."""
    await dispose()  # drop any engine cached against a previous URL
    await create_all()
    yield
    await dispose()


@pytest.fixture
def ledger():
    """Stub providers wired in, with a shared call ledger to assert against."""
    bundle, recorder = build_stub_bundle()
    set_bundle(bundle)
    yield recorder
    set_bundle(None)


@pytest.fixture
def store():
    session_store = MemorySessionStore(3600)
    set_store(session_store)
    yield session_store
    set_store(None)


@pytest.fixture
async def accounts(db):
    """A small fixture world covering the account states that change outcomes."""
    created: dict[str, str] = {}
    now = utcnow()
    async with session_scope() as session:
        # active pro account, one fresh small invoice
        ada = Customer(email="ada@example.com", full_name="Ada Lovelace", tier="pro")
        session.add(ada)
        await session.flush()
        ada_acct = Account(
            customer_id=ada.id, status="active", plan="pro", mrr_cents=9900, seats=5
        )
        session.add(ada_acct)
        await session.flush()
        fresh = Invoice(
            account_id=ada_acct.id, amount_cents=9900, status="paid",
            description="Pro plan", issued_at=now - timedelta(days=5),
        )
        session.add_all(
            [fresh, Entitlement(account_id=ada_acct.id, key="api_access", enabled=False)]
        )
        await session.flush()
        created |= {
            "ada_customer": ada.id,
            "ada_account": ada_acct.id,
            "ada_invoice": fresh.id,
        }

        # enterprise account with an invoice far above the autonomous ceiling
        grace = Customer(email="grace@example.com", full_name="Grace Hopper", tier="enterprise")
        session.add(grace)
        await session.flush()
        grace_acct = Account(
            customer_id=grace.id, status="active", plan="enterprise",
            mrr_cents=250_000, seats=100,
        )
        session.add(grace_acct)
        await session.flush()
        big = Invoice(
            account_id=grace_acct.id, amount_cents=250_000, status="paid",
            description="Enterprise plan", issued_at=now - timedelta(days=2),
        )
        session.add(big)
        await session.flush()
        created |= {
            "grace_customer": grace.id,
            "grace_account": grace_acct.id,
            "grace_invoice": big.id,
        }

        # free plan, used for feature-entitlement refusals
        alan = Customer(email="alan@example.com", full_name="Alan Turing", tier="standard")
        session.add(alan)
        await session.flush()
        alan_acct = Account(customer_id=alan.id, status="active", plan="free", seats=1)
        session.add(alan_acct)
        await session.flush()
        created |= {"alan_customer": alan.id, "alan_account": alan_acct.id}

        # fraud hold: every customer-facing write must be refused
        mallory = Customer(email="mallory@example.com", full_name="Mallory Quinn")
        session.add(mallory)
        await session.flush()
        mallory_acct = Account(
            customer_id=mallory.id, status="fraud_hold", plan="pro", mrr_cents=9900
        )
        session.add(mallory_acct)
        await session.flush()
        mallory_invoice = Invoice(
            account_id=mallory_acct.id, amount_cents=9900, status="paid",
            issued_at=now - timedelta(days=1),
        )
        session.add(mallory_invoice)
        await session.flush()
        created |= {
            "mallory_customer": mallory.id,
            "mallory_account": mallory_acct.id,
            "mallory_invoice": mallory_invoice.id,
        }

    return created
