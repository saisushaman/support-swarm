"""Liveness, readiness and introspection."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ... import __version__
from ...config import get_settings
from ...core.policy import ROLE_SCOPES
from ...db.base import session_scope
from ...session.store import get_store
from ...tools.base import registry
from ..schemas import HealthOut, ReadinessOut

router = APIRouter(tags=["ops"])


@router.get("/healthz", response_model=HealthOut, summary="Liveness")
async def healthz() -> HealthOut:
    s = get_settings()
    return HealthOut(
        status="ok",
        version=__version__,
        orchestrator=s.orchestrator,
        provider_mode=s.provider_mode,
        llm_enabled=s.llm_enabled,
    )


@router.get("/readyz", response_model=ReadinessOut, summary="Readiness")
async def readyz() -> ReadinessOut:
    checks: dict[str, str] = {}

    db_ok = True
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        db_ok = False
        checks["database"] = f"error: {exc!s}"[:200]

    session_ok = True
    try:
        session_ok = await get_store().ping()
        checks["session_store"] = "ok" if session_ok else "unreachable"
    except Exception as exc:
        session_ok = False
        checks["session_store"] = f"error: {exc!s}"[:200]

    return ReadinessOut(
        status="ok" if db_ok and session_ok else "degraded",
        database=db_ok,
        session_store=session_ok,
        checks=checks,
    )


@router.get("/v1/capabilities", summary="Registered tools and scope grants")
async def capabilities() -> dict:
    """What this deployment can actually do, and who is allowed to do it."""
    return {
        "tools": registry.describe(),
        "role_scopes": {
            role.value: sorted(s.value for s in scopes)
            for role, scopes in ROLE_SCOPES.items()
        },
        "limits": {
            "refund_auto_approve_cents": get_settings().refund_auto_approve_cents,
            "refund_window_days": get_settings().refund_window_days,
            "refund_daily_total_cap_cents": get_settings().refund_daily_total_cap_cents,
            "allow_autonomous_financial_actions":
                get_settings().allow_autonomous_financial_actions,
        },
    }
