"""Typed failures. Each maps to a stable HTTP status and a machine code."""

from __future__ import annotations


class SwarmError(Exception):
    """Base class for every expected failure in the swarm."""

    code = "swarm_error"
    http_status = 500

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class NotFoundError(SwarmError):
    code = "not_found"
    http_status = 404


class ValidationError(SwarmError):
    code = "invalid_request"
    http_status = 422


class AuthError(SwarmError):
    code = "unauthorized"
    http_status = 401


class PermissionDenied(SwarmError):
    """A guardrail refused the action. Never retry blindly."""

    code = "permission_denied"
    http_status = 403


class ApprovalRequired(SwarmError):
    """The action is legitimate but exceeds autonomous authority."""

    code = "approval_required"
    http_status = 409


class ProviderError(SwarmError):
    code = "provider_error"
    http_status = 502


class GuardrailTripped(SwarmError):
    code = "guardrail_tripped"
    http_status = 409


class StepBudgetExceeded(SwarmError):
    code = "step_budget_exceeded"
    http_status = 500
