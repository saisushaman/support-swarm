"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import get_settings
from ..core.errors import SwarmError
from ..core.logging import bind, configure, get_logger, restore
from ..db.base import create_all, dispose
from ..providers import close_bundle
from ..session.store import close_store
from .routes import escalations, health, tickets

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure()
    settings = get_settings()
    # Convenient for dev and CI; production applies db/sql/001_schema.sql via
    # migration and this is a no-op against an existing schema.
    if settings.database_url.startswith("sqlite"):
        await create_all()
    # Build the configured orchestrator once up front: a deployment that names
    # an orchestrator it cannot construct should refuse to start, not accept
    # traffic and escalate every ticket.
    from ..agents.service import build_orchestrator

    orchestrator = build_orchestrator()

    log.info(
        "support swarm starting",
        extra={
            "version": __version__,
            "orchestrator": orchestrator.name,
            "provider_mode": settings.provider_mode,
            "llm_enabled": settings.llm_enabled,
        },
    )
    try:
        yield
    finally:
        await close_bundle()
        await close_store()
        await dispose()
        log.info("support swarm stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Autonomous Customer Support Swarm",
        version=__version__,
        description=(
            "A hierarchical multi-agent support swarm. Agents look up records, "
            "verify entitlement, execute account and billing actions through "
            "policed tools, and escalate to humans with a pre-compiled "
            "diagnostic summary when a case is out of autonomous scope."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(SwarmError)
    async def swarm_error_handler(_: Request, exc: SwarmError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.as_dict())

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        """Bind a request id so every log line in the request is joinable."""
        request_id = request.headers.get("X-Request-Id") or f"req_{id(request):x}"
        previous = bind(request_id=request_id, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            restore(previous)
        response.headers["X-Request-Id"] = request_id
        return response

    app.include_router(health.router)
    app.include_router(tickets.router)
    app.include_router(escalations.router)
    return app


app = create_app()
