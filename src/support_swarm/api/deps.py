"""API authentication and scope enforcement.

API keys carry scopes, and scopes gate endpoints the same way agent roles gate
tools: ``agent`` may file and resolve tickets, ``admin`` may approve parked
actions and read the escalation queue. Approving your own bot's refund should
require a different credential than filing a ticket does.

Comparison is constant-time, and an unset/blank key never authenticates.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header

from ..config import get_settings
from ..core.errors import AuthError

SCOPE_AGENT = "agent"
SCOPE_ADMIN = "admin"


def _resolve_scopes(presented: str) -> set[str]:
    configured = get_settings().api_key_scopes()
    for key, scopes in configured.items():
        # Compare against every entry so a wrong key cannot be distinguished
        # from a wrong length by timing.
        if secrets.compare_digest(presented, key):
            return scopes
    return set()


async def caller_scopes(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> set[str]:
    if not x_api_key or not x_api_key.strip():
        raise AuthError("missing X-API-Key header")
    scopes = _resolve_scopes(x_api_key)
    if not scopes:
        raise AuthError("invalid API key")
    return scopes


def require_scope(scope: str):
    """Dependency factory: demand a specific scope."""

    async def _guard(
        scopes: Annotated[set[str], Depends(caller_scopes)],
    ) -> set[str]:
        if scope not in scopes:
            raise AuthError(f"api key lacks required scope {scope!r}")
        return scopes

    return _guard


RequireAgent = Annotated[set, Depends(require_scope(SCOPE_AGENT))]
RequireAdmin = Annotated[set, Depends(require_scope(SCOPE_ADMIN))]
