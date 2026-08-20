"""The agent swarm."""

from __future__ import annotations

from .intents import Intent, classify
from .pipeline import Resolution, ResolutionStatus, RunContext, Stage
from .service import (
    add_followup,
    approve_pending_action,
    build_orchestrator,
    intake_and_resolve,
    intake_ticket,
    resolve_ticket,
    ticket_transcript,
)
from .supervisor import DeterministicOrchestrator, SupervisorAgent
from .workers import (
    ApiActionAgent,
    DatabaseQueryAgent,
    EscalationAgent,
    PermissionVerifierAgent,
    QualityAssuranceAgent,
)

__all__ = [
    "ApiActionAgent",
    "DatabaseQueryAgent",
    "DeterministicOrchestrator",
    "EscalationAgent",
    "Intent",
    "PermissionVerifierAgent",
    "QualityAssuranceAgent",
    "Resolution",
    "ResolutionStatus",
    "RunContext",
    "Stage",
    "SupervisorAgent",
    "add_followup",
    "approve_pending_action",
    "build_orchestrator",
    "classify",
    "intake_and_resolve",
    "intake_ticket",
    "resolve_ticket",
    "ticket_transcript",
]
