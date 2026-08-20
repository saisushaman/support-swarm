"""Demo dataset.

Chosen so that every interesting branch of the pipeline is reachable from the
seeded data alone: a clean happy path, a refund above the autonomous ceiling, an
out-of-window invoice, a suspended account, a fraud hold, and a plan that does
not include the feature being asked for.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Account, Customer, Entitlement, Invoice, utcnow

SEED_MARKER_EMAIL = "ada@example.com"


async def already_seeded(session: AsyncSession) -> bool:
    stmt = select(Customer.id).where(Customer.email == SEED_MARKER_EMAIL).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def seed(session: AsyncSession, *, force: bool = False) -> dict[str, str]:
    """Insert the demo dataset. Idempotent unless ``force``."""
    if not force and await already_seeded(session):
        return {"status": "already_seeded"}

    now = utcnow()
    created: dict[str, str] = {}

    # 1. Happy path: pro plan, active, small refundable invoice.
    ada = Customer(email=SEED_MARKER_EMAIL, full_name="Ada Lovelace", tier="pro")
    session.add(ada)
    await session.flush()
    ada_account = Account(
        customer_id=ada.id, status="active", plan="pro", mrr_cents=9900, seats=5,
        mfa_enabled=True,
    )
    session.add(ada_account)
    await session.flush()
    session.add_all(
        [
            Invoice(
                account_id=ada_account.id, amount_cents=9900, status="paid",
                description="Pro plan - monthly", issued_at=now - timedelta(days=6),
            ),
            Invoice(
                account_id=ada_account.id, amount_cents=9900, status="paid",
                description="Pro plan - monthly", issued_at=now - timedelta(days=36),
            ),
            Entitlement(account_id=ada_account.id, key="api_access", enabled=False),
            Entitlement(account_id=ada_account.id, key="dark_mode", enabled=True),
        ]
    )
    created["ada"] = ada.id

    # 2. Large invoice: refund lands above the autonomous ceiling -> approval.
    grace = Customer(email="grace@example.com", full_name="Grace Hopper", tier="enterprise")
    session.add(grace)
    await session.flush()
    grace_account = Account(
        customer_id=grace.id, status="active", plan="enterprise", mrr_cents=250_000,
        seats=120, mfa_enabled=True,
    )
    session.add(grace_account)
    await session.flush()
    session.add_all(
        [
            Invoice(
                account_id=grace_account.id, amount_cents=250_000, status="paid",
                description="Enterprise plan - monthly", issued_at=now - timedelta(days=3),
            ),
            Entitlement(account_id=grace_account.id, key="sso", enabled=True),
        ]
    )
    created["grace"] = grace.id

    # 3. Out-of-window invoice only -> refund held for a human.
    alan = Customer(email="alan@example.com", full_name="Alan Turing", tier="standard")
    session.add(alan)
    await session.flush()
    alan_account = Account(
        customer_id=alan.id, status="active", plan="free", mrr_cents=0, seats=1
    )
    session.add(alan_account)
    await session.flush()
    session.add(
        Invoice(
            account_id=alan_account.id, amount_cents=4900, status="paid",
            description="Legacy plan", issued_at=now - timedelta(days=200),
        )
    )
    created["alan"] = alan.id

    # 4. Suspended account -> writes above low risk need a human.
    katherine = Customer(
        email="katherine@example.com", full_name="Katherine Johnson", tier="standard"
    )
    session.add(katherine)
    await session.flush()
    katherine_account = Account(
        customer_id=katherine.id, status="suspended", plan="pro", mrr_cents=9900, seats=3
    )
    session.add(katherine_account)
    created["katherine"] = katherine.id

    # 5. Fraud hold -> every write denied outright.
    mallory = Customer(email="mallory@example.com", full_name="Mallory Quinn", tier="standard")
    session.add(mallory)
    await session.flush()
    mallory_account = Account(
        customer_id=mallory.id, status="fraud_hold", plan="pro", mrr_cents=9900, seats=2
    )
    session.add(mallory_account)
    await session.flush()
    session.add(
        Invoice(
            account_id=mallory_account.id, amount_cents=9900, status="paid",
            description="Pro plan - monthly", issued_at=now - timedelta(days=2),
        )
    )
    created["mallory"] = mallory.id

    await session.flush()
    return {"status": "seeded", **created}
