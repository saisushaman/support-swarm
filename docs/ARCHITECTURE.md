# Architecture

## The problem this shape solves

An FAQ bot that answers wrongly wastes a customer's time. An *operational* agent
that acts wrongly issues a refund that did not exist, resets the wrong person's
password, or leaks one customer's data to another. The failure modes are
categorically different, so the architecture is organised around one question:

> When the model is wrong — or the ticket is written by someone trying to
> manipulate it — what stops the damage?

The answer is that **the model never holds the authority**. Agents produce
proposals. A pure, deny-by-default policy engine decides. Every side effect flows
through one audited choke point.

## Request lifecycle

```
                       POST /v1/tickets
                              │
                    ┌─────────▼──────────┐
                    │ intake             │  persist ticket, scan for injection,
                    │ (service.py)       │  link customer record
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ triage             │  Supervisor: intent + plan
                    │ (supervisor.py)    │  scope: db:read only
                    └─────────┬──────────┘
                              │  not autonomously resolvable ─────────┐
                    ┌─────────▼──────────┐                            │
                    │ gather             │  Database Query agent      │
                    │ (workers.py)       │  read-only tools           │
                    └─────────┬──────────┘                            │
                              │  customer not found ─────────────────►│
                    ┌─────────▼──────────┐                            │
                    │ verify             │  Permission Verifier       │
                    │                    │  entitlement + blockers    │
                    └─────────┬──────────┘                            │
                              │  not entitled ───────────────────────►│
                    ┌─────────▼──────────┐                            │
                    │ act                │  API Action agent          │
                    │                    │  ── ToolRunner ──┐         │
                    └─────────┬──────────┘                  │         │
                              │                     ┌───────▼──────┐  │
                              │                     │ policy       │  │
                              │                     │ engine       │  │
                              │                     └───────┬──────┘  │
                              │        DENY / APPROVAL_REQUIRED ─────►│
                    ┌─────────▼──────────┐                            │
                    │ QA                 │  veto power                │
                    └─────────┬──────────┘                            │
                              │  veto ───────────────────────────────►│
                    ┌─────────▼──────────┐                  ┌─────────▼────────┐
                    │ respond            │                  │ escalate         │
                    │ status: resolved   │                  │ diagnostic brief │
                    └────────────────────┘                  │ + human queue    │
                                                            └──────────────────┘
```

Six of the eight stages can terminate in an escalation. That is the design: an
autonomous system whose only exit is "success" will eventually force a bad
success.

## Layers

| Layer | Module | Rule it enforces |
|---|---|---|
| API | `api/` | Scoped API keys; approving an action needs a stronger credential than filing a ticket |
| Service | `agents/service.py` | Transaction boundaries, timeouts, fail-open-to-human |
| Orchestration | `agents/supervisor.py`, `agents/crew.py` | Stage sequencing; both share one execution path |
| Agents | `agents/workers.py` | One job, one privilege set each |
| Tools | `tools/` | **The choke point.** Policy, idempotency and audit on every action |
| Policy | `core/policy.py` | Pure decisions. No I/O, no model, no exceptions to it |
| Providers | `providers/` | Vendor isolation; stub and HTTP behind one protocol |
| Storage | `db/`, `session/` | System of record + append-only audit; Redis working memory |

### Why the tool layer is a choke point, not a convenience

`ToolRunner.run` is the only way to reach a provider or mutate a row. It always:

1. resolves the tool spec — an unknown name is a denial, not a passthrough;
2. assembles policy facts **from the database**, never from the model's claims;
3. checks for an idempotent replay *before* evaluating policy;
4. evaluates the guardrails;
5. executes only on `ALLOW`;
6. writes an `action_log` row and a ticket event — including for refusals.

Step 3's ordering is load-bearing and was a real bug during development. A
completed refund changes the refundable balance, so re-evaluating policy on a
retry denies it for a limit its own first execution created — turning "your
refund went through" into "your refund was refused". Replay must win.

Step 6 means the audit log answers *"why didn't the bot refund this?"*, which in
practice is asked more often than the opposite.

## The two orchestrators

Both implement `resolve(ctx) -> Resolution` and share `finish_from_verified` for
act → QA → respond, so the guarantees cannot drift between them.

**`deterministic`** — explicit control flow, no model calls. This is the
reference implementation and what CI exercises. Every guardrail test runs against
it, which means the guarantees are verified without a network or a model.

**`crewai`** — a hierarchical CrewAI crew (manager + Database Query + Permission
Verifier) reasons over the read-only tools, bridged onto the event loop. Its
output is a **proposal**, and the pipeline then re-derives facts from the
database before acting. The crew may *add* blockers; it can never clear one. A
jailbroken crew therefore produces a bad proposal, not a bad outcome.

## Threat model

| Threat | Mitigation | Verified by |
|---|---|---|
| Prompt injection in the ticket body | Untrusted content is fenced; injection is scored; hostile tickets lose write authority entirely | `test_hostile_injection_*` |
| Model hallucinates an entitlement or amount | Facts always re-read from the DB; policy is model-independent | `test_policy.py` |
| Attacker redirects a password reset | Reset targets the address on the *record*, never the ticket | `test_password_reset_targets_the_record_address` |
| Attacker names another customer's invoice | Invoice/account ownership is checked in policy | `test_refund_against_another_accounts_invoice` |
| Runaway or duplicated refunds | Per-action ceiling, rolling 24h cap, deterministic idempotency keys | `test_repeating_the_same_refund_replays` |
| Compromised agent escalating privilege | Per-role scope grants; the Supervisor cannot write at all | `test_only_the_api_action_agent_can_move_money` |
| PII in logs, audit rows or model prompts | Redaction at all three boundaries | `test_pii_is_redacted_before_it_reaches_the_audit_log` |
| Silent failure — a ticket nobody sees | Escalation is `INTERNAL` risk and cannot be blocked; crashes and timeouts fail open to a human | `test_fraud_hold_account_is_refused_and_still_escalated` |
| Wrong reply reaching the customer | QA veto; a model may only tighten its verdict | `test_downstream_failure_escalates_rather_than_lying` |

### What is deliberately *not* mitigated

* A malicious operator with an `admin` API key can approve anything the policy
  engine would otherwise hold. Approval raises authority to the human's level —
  it does not bypass outright denials, but it is a trusted role.
* The injection scanner is a pattern-based heuristic. It is defence in depth, not
  a boundary; the boundary is that authority does not come from text at all.
* Rate limiting and tenant isolation are assumed to live at the gateway.

## Data model

`customers` → `accounts` → (`invoices`, `entitlements`) is ordinary domain state.
The interesting tables are append-only:

* **`ticket_events`** — the transcript: every customer message, agent conclusion,
  tool call and policy decision, sequenced per ticket. Nothing updates it.
* **`action_log`** — one row per *attempted* mutation, with the decision that
  produced it, the amount, and the approver if any. The unique index on
  `idempotency_key` is what makes replay detection a database guarantee rather
  than an application convention.

Together they let you replay exactly what each agent saw and did, which is the
minimum bar for letting software move money.

## Operational notes

* **Failure isolation** — a provider exception is caught and becomes a failed
  tool result, not a crashed resolution. A crashed *resolution* is caught by
  `_fail_open`, which writes the escalation on a fresh transaction because the
  in-flight one may be poisoned.
* **Timeouts** — resolution is bounded by `SWARM_RESOLUTION_TIMEOUT_SECONDS`;
  exceeding it escalates rather than hanging the request.
* **Sessions** — Redis holds working memory keyed by ticket, so an approval that
  arrives minutes later resumes with context. `memory://` is process-local and
  suitable only for single-worker or test use.
* **Observability** — every log line is JSON with a bound `request_id` and
  `ticket_id`, and passes through redaction on the way out.
