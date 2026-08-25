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
  produces a bad proposal, not a bad outcome — and that the crew can only reach
  read-only tools is asserted in CI, not merely intended. Needs
  `pip install -e ".[crewai]"` and **Python 3.11–3.13**; CrewAI does not support
  3.14 yet.

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

183 tests. They cover the guardrails directly — refund ceilings and windows,
scope denial, fraud-hold refusal, identity mismatch, injection containment,
idempotent replay, QA veto — plus each end-to-end terminal state, the API
contract, and the real HTTP provider path.

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

## Status: what is verified, and what is not

**Verified end to end** (183 tests, ruff clean — the 6 CrewAI tests skip off-CI):

* the full pipeline over HTTP — file a ticket, get a resolved/held/escalated
  outcome, read the audit transcript;
* every terminal state: resolved, held for approval, denied, escalated, failed;
* the hold → escalate → human-approve → execute → reply loop;
* all guardrails, including boundary cases on both refund limits;
* the real REST provider path (`providers/http.py`) against a live ASGI service,
  including that a declined refund is never retried;
* the LLM gateway's parsing and retry classification;
* the Docker image: CI builds it, starts it, and waits for `/healthz`;
* Python 3.11, 3.12 and 3.13 in CI (development happened on 3.14);
* the CrewAI wiring, in the one place it can run — CI on 3.12 with the extra
  installed. It asserts the crew is hierarchical with the Supervisor managing,
  only the manager delegates, the ticket body arrives fenced as untrusted, a
  bridged tool really executes through the policed runner, and **every tool the
  crew can reach is `Risk.READ`** (`issue_refund`, `reset_password` and
  `set_entitlement` asserted absent). That job caught the bridge failing to
  construct at all on its very first run.

**Not executed anywhere yet:**

* **A full crew run against a live model.** The wiring is tested; `kickoff()`
  with real credentials is not. CrewAI supports Python `<3.14`, so if you develop
  on 3.14 the extra is a no-op locally and CI is your only signal.
* **Live LiteLLM calls.** `SWARM_LLM_ENABLED=true` routes through the gateway;
  its parsing and retry logic are tested, but no provider has been called.
* **Postgres and Redis via `docker compose`.** The image itself is verified — CI
  builds it, starts it and waits for `/healthz` — but the multi-service compose
  stack has not been brought up.

**Known limits by design:**

* Providers are stubs by default. `SWARM_PROVIDER_MODE=http` expects endpoints
  matching the shapes in `providers/http.py`.
* The intent classifier is a small rule set on purpose — the subject here is the
  guardrail architecture, not state-of-the-art classification.
* `MemorySessionStore` is process-local; multi-worker deployments need Redis.
* An `admin` API key can approve anything the engine would hold. It cannot
  override an outright denial, but it is a trusted role.
