"""Real-time session state.

The swarm keeps its working memory here -- the plan, gathered facts, the
conversation turns, and any action parked awaiting human approval -- so that a
follow-up message or an approval callback minutes later resumes with full
context instead of re-deriving everything.

Redis in production; an in-process dict when ``SWARM_SESSION_URL=memory://``
so tests and a laptop run need no daemon. Both honour the same TTL contract.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import get_settings
from ..core.logging import get_logger

log = get_logger(__name__)

_KEY_PREFIX = "swarm:session:"


@dataclass
class SessionState:
    """Working memory for one ticket."""

    ticket_id: str
    intent: str | None = None
    step: int = 0
    turns: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    plan: list[str] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    injection: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, int] = field(default_factory=dict)

    def add_turn(self, role: str, content: str, **extra: Any) -> None:
        self.turns.append({"role": role, "content": content, **extra})

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> SessionState:
        data = json.loads(raw)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class SessionStore(ABC):
    @abstractmethod
    async def get(self, ticket_id: str) -> SessionState | None: ...

    @abstractmethod
    async def put(self, state: SessionState) -> None: ...

    @abstractmethod
    async def delete(self, ticket_id: str) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def load_or_create(self, ticket_id: str) -> SessionState:
        return await self.get(ticket_id) or SessionState(ticket_id=ticket_id)

    async def ping(self) -> bool:
        return True


class MemorySessionStore(SessionStore):
    """Process-local store with real TTL semantics. Not for multi-worker prod."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def get(self, ticket_id: str) -> SessionState | None:
        async with self._lock:
            entry = self._data.get(ticket_id)
            if entry is None:
                return None
            expires_at, raw = entry
            if expires_at < time.monotonic():
                self._data.pop(ticket_id, None)
                return None
            return SessionState.from_json(raw)

    async def put(self, state: SessionState) -> None:
        async with self._lock:
            self._data[state.ticket_id] = (
                time.monotonic() + self._ttl,
                state.to_json(),
            )

    async def delete(self, ticket_id: str) -> None:
        async with self._lock:
            self._data.pop(ticket_id, None)

    async def close(self) -> None:
        async with self._lock:
            self._data.clear()


class RedisSessionStore(SessionStore):
    """Redis-backed store. ``redis`` is imported lazily so it stays optional."""

    def __init__(self, url: str, ttl_seconds: int) -> None:
        try:
            from redis.asyncio import from_url  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "SWARM_SESSION_URL points at Redis but the 'redis' package is not "
                "installed. Install with: pip install 'support-swarm[redis]'"
            ) from exc
        self._client = from_url(url, encoding="utf-8", decode_responses=True)
        self._ttl = ttl_seconds

    @staticmethod
    def _key(ticket_id: str) -> str:
        return f"{_KEY_PREFIX}{ticket_id}"

    async def get(self, ticket_id: str) -> SessionState | None:
        raw = await self._client.get(self._key(ticket_id))
        return SessionState.from_json(raw) if raw else None

    async def put(self, state: SessionState) -> None:
        await self._client.set(
            self._key(state.ticket_id), state.to_json(), ex=self._ttl
        )

    async def delete(self, ticket_id: str) -> None:
        await self._client.delete(self._key(ticket_id))

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("redis ping failed", extra={"error": str(exc)})
            return False

    async def close(self) -> None:
        await self._client.aclose()


_store: SessionStore | None = None


def build_store(url: str | None = None, ttl_seconds: int | None = None) -> SessionStore:
    settings = get_settings()
    url = url or settings.session_url
    ttl = ttl_seconds if ttl_seconds is not None else settings.session_ttl_seconds
    if url.startswith("redis://") or url.startswith("rediss://"):
        return RedisSessionStore(url, ttl)
    if url.startswith("memory://"):
        return MemorySessionStore(ttl)
    raise ValueError(f"Unsupported session_url scheme: {url!r}")


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = build_store()
    return _store


def set_store(store: SessionStore | None) -> None:
    """Test hook / DI override."""
    global _store
    _store = store


async def close_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
    _store = None
