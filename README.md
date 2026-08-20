# Autonomous Customer Support Swarm

A hierarchical multi-agent support system that does the operational work behind a
ticket — looks up customer and billing records, verifies entitlement, executes
password resets, refunds and entitlement changes through policed APIs, and hands
anything out of its authority to a human with a pre-compiled diagnostic summary.

It is not an FAQ bot. The interesting part is not the answering; it is that an
autonomous agent is allowed to move money and reset credentials **and cannot
exceed its authority even when the model is wrong or the ticket is hostile.**

```
POST /v1/tickets ─▶ intake ─▶ triage ─▶ gather ─▶ verify ─▶ act ─▶ QA ─▶ respond
                                 │                             │        │
                                 └── out of scope ─────────────┴────────┴─▶ escalate
                                                                            (diagnostic
                                                                             summary +
                                                                             human queue)
```

## The core design decision

Every mutating action in the system passes through one function
([`core/policy.py`](src/support_swarm/core/policy.py)) that is **pure, deny-by-default,
and independent of the LLM**. Agents *propose*; the policy engine *decides*.

That inversion is what makes the rest safe. A model — or a prompt injection
inside a ticket body — can ask for a $5,000 refund. It cannot get one, because
the decision is made by code that never reads the ticket:

| Guardrail | Where |
|---|---|
| Least-privilege scopes per agent role — the Database Query agent has no write scope at all | `core/policy.py` |
| Three-valued outcomes: `ALLOW` / `APPROVAL_REQUIRED` / `DENY` | `core/policy.py` |
| Refund ceiling, refund window, rolling 24h refund cap | `core/policy.py` |
| Identity verification before any credential change | `tools/base.py` |
| Prompt-injection scoring; hostile tickets lose autonomous authority | `core/injection.py` |
| Deterministic idempotency keys — a replayed resolution cannot double-refund | `tools/base.py` |
| Append-only audit of every call **including refusals** | `db/models.py` |
| PII redaction on every log line, audit row and LLM prompt | `core/redaction.py` |
| QA agent with veto power before any reply reaches a customer | `agents/workers.py` |

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[dev]"
```

Run the demo. It seeds a dataset and walks nine tickets chosen to land on
*different terminal states* — resolved, held for approval, denied, escalated:

```bash
SWARM_DATABASE_URL="sqlite+aiosqlite:///./demo.db" python -m support_swarm demo
```

Run the API:

```bash
uvicorn support_swarm.api.main:app --reload
```

```bash
curl -s localhost:8000/v1/tickets -H 'X-API-Key: dev-key' -H 'content-type: application/json' -d '{"requester_email":"ada@example.com","subject":"Cannot log in","body":"I forgot my password, please send a reset link."}'
```

Interactive docs at `http://localhost:8000/docs`. `GET /v1/capabilities` reports
exactly which tools this deployment has and what the live limits are.

## Full stack with Docker

```bash
docker compose up --build
```

Brings up PostgreSQL, Redis and the API, seeds the demo data, and runs against
stub providers so nothing external is touched.

## The agents

| Agent | Job | Privileges |
|---|---|---|
| **Supervisor** | Triage, plan, delegate | `db:read` only — a hijacked supervisor still cannot act |
| **Database Query** | Gather facts from the system of record | read-only |
| **Permission Verifier** | Is this requester entitled to this action? | read-only |
| **API Action** | Execute the plan via policed tools | identity / billing / entitlement writes |
| **Quality Assurance** | Last gate before the customer sees anything | read-only, holds a **veto** |
| **Escalation** | Compile the human handoff | escalation create |

### Two orchestrators

* `SWARM_ORCHESTRATOR=deterministic` (default) — the reference pipeline. Explicit
  control flow, no model calls, fully deterministic. This is what CI exercises.
* `SWARM_ORCHESTRATOR=crewai` — a hierarchical CrewAI crew (manager + workers)
  does the *analysis* with the read-only tools bridged in. Its output is a
  **proposal**; execution returns to the same policed path. A jailbroken crew
  produces a bad proposal, not a bad outcome. Needs `pip install -e ".[crewai]"`.

Set `SWARM_LLM_ENABLED=true` to route triage and QA judgement through LiteLLM
(`llm/gateway.py`). The model can only *tighten* a QA verdict, never loosen one,
and a model outage falls back to the deterministic path instead of failing the
ticket.

## Escalation is a product feature, not an error path

When the swarm stops, a human gets a compiled brief rather than a raw transcript:
what was asked, what was verified, account state, the target invoice, every
action attempted with its policy reason, injection warnings, QA findings, and a
concrete suggested next step.

```bash
python -m support_swarm escalations --summary
```

Parked actions resume with one call — facts are re-read and guardrails re-run, so
an approval granted at 09:00 cannot execute against 14:00 state:

```bash
curl -s -X POST localhost:8000/v1/tickets/$TICKET/approve \
  -H 'X-API-Key: dev-admin-key' -H 'content-type: application/json' \
  -d '{"approver":"oncall@example.com"}'
```

## Tests

```bash
pytest
```

The suite covers the guardrails directly — refund ceilings and windows, scope
denial, fraud-hold refusal, identity mismatch, injection containment, idempotent
replay, QA veto — plus each end-to-end terminal state and the API contract.

## Configuration

Every setting is an env var prefixed `SWARM_`; see [`.env.example`](.env.example).
Defaults are the safe ones: stub providers, deterministic orchestrator, no model
calls, financial actions capped.

## Layout

```
src/support_swarm/
  config.py          settings (pydantic-settings)
  core/              policy engine, injection scanner, redaction, logging, errors
  db/                models, repository, seed data, schema DDL
  session/           Redis session state (memory:// fallback)
  llm/               LiteLLM gateway + role prompts
  providers/         identity / billing / entitlement / notification (stub + http)
  tools/             the policed tool catalogue and the ToolRunner
  agents/            supervisor, workers, crew, pipeline contract, service layer
  api/               FastAPI app, routes, auth
  cli.py             init-db / seed / demo / resolve / escalations
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the request lifecycle and
the threat model, and [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) for every rule
the policy engine enforces and why.

## Status and limits

Honest about what this is: the domain APIs behind it are stub providers by
default, so `SWARM_PROVIDER_MODE=http` needs real identity/billing endpoints
matching `providers/http.py`. The intent classifier is deliberately a small rule
set — the point of the project is the guardrail architecture, not
state-of-the-art classification. `MemorySessionStore` is process-local, so
multi-worker deployments need Redis.
