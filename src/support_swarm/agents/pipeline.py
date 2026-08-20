"""Shared contract between the orchestrators and the specialist agents.

Both the deterministic supervisor and the CrewAI crew produce a
:class:`Resolution` and drive the same seven stages, so swapping orchestrators
changes *how* the swarm reasons, never what the rest of the system can expect
from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.injection import InjectionVerdict
from ..db.models import Ticket
from ..providers.base import ProviderBundle
from ..session.store import SessionState, SessionStore
from ..tools.base import ToolResult, ToolRunner
from .intents import Classification


class Stage(StrEnum):
    INTAKE = "intake"
    TRIAGE = "triage"
    GATHER = "gather"
    VERIFY = "verify"
    ACT = "act"
    QA = "qa"
    RESPOND = "respond"
    ESCALATE = "escalate"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"


@dataclass
class Verification:
    """What the Permission Verifier concluded."""

    identity_verified: bool = False
    entitled: bool = False
    blockers: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity_verified": self.identity_verified,
            "entitled": self.entitled,
            "blockers": list(self.blockers),
            "evidence": dict(self.evidence),
        }


@dataclass
class QAVerdict:
    verdict: str = "pass"  # pass | revise | escalate
    issues: list[str] = field(default_factory=list)
    revised_reply: str | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "issues": list(self.issues),
            "revised": self.revised_reply is not None,
        }


@dataclass
class RunContext:
    """Everything a stage needs. Threaded through the whole resolution."""

    ticket: Ticket
    runner: ToolRunner
    providers: ProviderBundle
    store: SessionStore
    state: SessionState
    injection: InjectionVerdict
    classification: Classification | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    verification: Verification = field(default_factory=Verification)
    actions: list[ToolResult] = field(default_factory=list)
    draft_reply: str = ""
    qa: QAVerdict = field(default_factory=QAVerdict)
    stages: list[str] = field(default_factory=list)

    def mark(self, stage: Stage) -> None:
        self.stages.append(stage.value)


@dataclass
class Resolution:
    """The single value every entry point returns."""

    ticket_id: str
    status: ResolutionStatus
    intent: str
    reply: str | None = None
    stages: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    escalation_id: str | None = None
    escalation_reason: str | None = None
    qa: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    injection: dict[str, Any] = field(default_factory=dict)
    pending_action: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "status": self.status.value,
            "intent": self.intent,
            "reply": self.reply,
            "stages": list(self.stages),
            "actions": list(self.actions),
            "escalation_id": self.escalation_id,
            "escalation_reason": self.escalation_reason,
            "qa": dict(self.qa),
            "verification": dict(self.verification),
            "injection": dict(self.injection),
            "pending_action": self.pending_action,
        }
