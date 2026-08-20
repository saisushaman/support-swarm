"""Escalation queue endpoints -- the human side of the handoff."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...db import repository as repo
from ...db.base import session_scope
from ..deps import RequireAdmin
from ..schemas import EscalationOut

router = APIRouter(prefix="/v1/escalations", tags=["escalations"])


@router.get("", response_model=list[EscalationOut], summary="List escalations")
async def list_escalations(
    _scopes: RequireAdmin,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EscalationOut]:
    async with session_scope() as session:
        rows = await repo.list_escalations(session, status=status_filter, limit=limit)
        return [
            EscalationOut(
                id=r.id,
                ticket_id=r.ticket_id,
                queue=r.queue,
                severity=r.severity,
                reason_code=r.reason_code,
                status=r.status,
                summary_md=r.summary_md,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ]
