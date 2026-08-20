"""Ticket endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, status

from ...agents import service
from ...core.logging import get_logger
from ..deps import RequireAdmin, RequireAgent
from ..schemas import (
    ApprovalRequest,
    FollowupCreate,
    ResolutionOut,
    TicketAccepted,
    TicketCreate,
)

log = get_logger(__name__)

router = APIRouter(prefix="/v1/tickets", tags=["tickets"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ResolutionOut | TicketAccepted,
    summary="File a ticket and (by default) resolve it",
)
async def create_ticket(
    payload: TicketCreate,
    background: BackgroundTasks,
    _scopes: RequireAgent,
) -> ResolutionOut | TicketAccepted:
    if payload.resolve_now:
        ticket_id, resolution = await service.intake_and_resolve(
            requester_email=str(payload.requester_email),
            subject=payload.subject,
            body=payload.body,
            channel=payload.channel,
            priority=payload.priority,
        )
        log.info(
            "ticket resolved",
            extra={"ticket_id": ticket_id, "status": resolution.status.value},
        )
        return ResolutionOut(**resolution.as_dict())

    # Fire-and-forget intake: persist first so the id we hand back is real, then
    # resolve out of band.
    from ...db.base import session_scope

    async with session_scope() as session:
        ticket = await service.intake_ticket(
            session,
            requester_email=str(payload.requester_email),
            subject=payload.subject,
            body=payload.body,
            channel=payload.channel,
            priority=payload.priority,
        )
        ticket_id = ticket.id

    background.add_task(service.resolve_ticket, ticket_id)
    return TicketAccepted(ticket_id=ticket_id)


@router.get(
    "/{ticket_id}",
    summary="Full audit transcript for one ticket",
)
async def get_ticket(ticket_id: str, _scopes: RequireAgent) -> dict:
    return await service.ticket_transcript(ticket_id)


@router.post(
    "/{ticket_id}/messages",
    response_model=ResolutionOut,
    summary="Add a customer follow-up and re-run resolution",
)
async def add_message(
    ticket_id: str, payload: FollowupCreate, _scopes: RequireAgent
) -> ResolutionOut:
    resolution = await service.add_followup(ticket_id, payload.message)
    return ResolutionOut(**resolution.as_dict())


@router.post(
    "/{ticket_id}/approve",
    response_model=ResolutionOut,
    summary="Approve a parked action (admin scope)",
)
async def approve(
    ticket_id: str, payload: ApprovalRequest, _scopes: RequireAdmin
) -> ResolutionOut:
    resolution = await service.approve_pending_action(ticket_id, payload.approver)
    log.info(
        "parked action approved",
        extra={
            "ticket_id": ticket_id,
            "approver": payload.approver,
            "status": resolution.status.value,
        },
    )
    return ResolutionOut(**resolution.as_dict())
