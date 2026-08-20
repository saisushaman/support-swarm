"""Downstream provider selection."""

from __future__ import annotations

from ..config import get_settings
from .base import (
    BillingProvider,
    EntitlementProvider,
    IdentityProvider,
    NotificationProvider,
    ProviderBundle,
    ProviderResult,
)

_bundle: ProviderBundle | None = None


def build_bundle() -> ProviderBundle:
    """Build a bundle according to ``SWARM_PROVIDER_MODE``."""
    if get_settings().provider_mode == "http":
        from .http import build_http_bundle

        return build_http_bundle()
    from .stub import build_stub_bundle

    bundle, _ledger = build_stub_bundle()
    return bundle


def get_bundle() -> ProviderBundle:
    global _bundle
    if _bundle is None:
        _bundle = build_bundle()
    return _bundle


def set_bundle(bundle: ProviderBundle | None) -> None:
    """Test hook / DI override."""
    global _bundle
    _bundle = bundle


async def close_bundle() -> None:
    global _bundle
    if _bundle is not None:
        await _bundle.aclose()
    _bundle = None


__all__ = [
    "BillingProvider",
    "EntitlementProvider",
    "IdentityProvider",
    "NotificationProvider",
    "ProviderBundle",
    "ProviderResult",
    "build_bundle",
    "close_bundle",
    "get_bundle",
    "set_bundle",
]
