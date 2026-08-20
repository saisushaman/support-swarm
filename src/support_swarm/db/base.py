"""Async engine / session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import get_settings
from .models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # SQLite has no pool tuning worth doing and needs same-thread relaxed.
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        _engine = create_async_engine(url, future=True, **_engine_kwargs(url))
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create the schema. Fine for dev/CI; production uses ``db/sql/001_schema.sql``."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
