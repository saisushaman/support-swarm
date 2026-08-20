"""Request / response models for the public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requester_email: EmailStr
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    channel: Literal["email", "chat", "phone", "web", "api"] = "email"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    #: When false the caller gets a ticket id immediately and resolution runs in
    #: the background. Use for high-volume intake.
    resolve_now: bool = True


class FollowupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)


class ResolutionOut(BaseModel):
    ticket_id: str
    status: str
    intent: str
    reply: str | None = None
    stages: list[str] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    escalation_id: str | None = None
    escalation_reason: str | None = None
    qa: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    injection: dict[str, Any] = Field(default_factory=dict)
    pending_action: dict[str, Any] | None = None


class TicketAccepted(BaseModel):
    ticket_id: str
    status: str = "accepted"
    detail: str = "resolution running in the background"


class EscalationOut(BaseModel):
    id: str
    ticket_id: str
    queue: str
    severity: str
    reason_code: str
    status: str
    summary_md: str
    created_at: str | None = None


class HealthOut(BaseModel):
    status: str
    version: str
    orchestrator: str
    provider_mode: str
    llm_enabled: bool


class ReadinessOut(BaseModel):
    status: str
    database: bool
    session_store: bool
    checks: dict[str, str] = Field(default_factory=dict)


class ErrorOut(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
