# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so code edits do not invalidate the install.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

# Run unprivileged: the process needs no write access to its own code, so /app
# stays root-owned. Anything the process writes goes to /data instead -- without
# a writable directory of its own, a SQLite-backed container cannot create its
# database file and fails at startup.
RUN useradd --create-home --uid 10001 swarm \
    && mkdir -p /data \
    && chown swarm:swarm /data

ENV SWARM_DATABASE_URL="sqlite+aiosqlite:////data/swarm.db"
VOLUME ["/data"]

USER swarm

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "support_swarm.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
