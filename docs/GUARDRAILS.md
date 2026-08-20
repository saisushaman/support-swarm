# Guardrails

Every rule the policy engine enforces, why it exists, and the test that proves
it. All of it lives in [`core/policy.py`](../src/support_swarm/core/policy.py),
which is pure — no I/O, no clock beyond the `now` it is handed, no model.

## Outcomes are three-valued

A binary allow/deny forces a bad choice: refuse legitimate work, or permit
things nobody sanctioned. So:

| Outcome | Meaning | What happens |
|---|---|---|
| `ALLOW` | Within autonomous authority | Executes |
| `APPROVAL_REQUIRED` | Legitimate, but beyond autonomous authority | Parked, escalated with a diagnostic, resumable by a human |
| `DENY` | Must not happen at all | Refused; approval cannot override it |

The distinction between the last two matters. A $2,500 refund on a real invoice
is *held*. A $2,500 refund against a $99 invoice is *denied* — no human approval
makes that money exist.

## Risk tiers

| Tier | Examples | Treatment |
|---|---|---|
| `READ` | `lookup_customer`, `list_invoices` | Scope-checked only; survives even a hostile ticket, because facts are how a human later understands the attack |
| `INTERNAL` | `create_escalation`, `send_customer_reply` | Scope-checked, idempotent, audited — but **never** blocked by account state or injection score |
| `LOW_WRITE` | `set_entitlement` | Full guardrails |
| `SENSITIVE_WRITE` | `reset_password`, `revoke_sessions` | Full guardrails + identity verification |
| `FINANCIAL` | `issue_refund` | Full guardrails + monetary rules |

### Why `INTERNAL` exists

This tier was added after a bug. Escalation is itself a write, so a ticket on a
`fraud_hold` account had its refund correctly denied — and then had *its own
escalation* denied by the same rule. Three of seven escalations silently
vanished in a demo run: exactly the tickets most needing a human were the ones
no human would ever see.

Escalating, and telling the customer a person is looking, are the **fail-safe**
path. Gating them on the conditions that trigger them is a design error.

## Least privilege

```
supervisor          db:read
database_query      db:read  billing:read
permission_verifier db:read  billing:read
api_action          db:read  billing:read  identity:write  billing:write
                    entitlement:write  notify:send
quality_assurance   db:read  billing:read
escalation          db:read  escalation:create  notify:send
```

Two consequences worth stating explicitly:

* The **Supervisor** — the agent that reads the untrusted ticket and does the
  most model reasoning — cannot write anything. A fully hijacked Supervisor
  cannot move money.
* The **API Action** agent cannot create escalations, so it cannot route around
  QA by escalating its own failures.

`GET /v1/capabilities` serves this table live, so the deployed grants are
inspectable rather than merely documented.

## Structural rules

| Rule | Reason |
|---|---|
| Unknown tool → `DENY` | Deny by default; a typo or hallucinated tool name is never a passthrough |
| Missing scope → `DENY` | Least privilege, enforced not advisory |
| Write without an idempotency key → `DENY` | An unkeyed write cannot be safely retried, so it is not allowed to exist |
| Non-idempotent tool → `DENY` | Reserved so such a tool cannot be added without a policy change |
| Write with no account context → `DENY` | Cannot verify what you cannot see |

## Account state

| State | Effect on writes |
|---|---|
| `active` | Normal |
| `suspended` | Anything above `LOW_WRITE` → `APPROVAL_REQUIRED` |
| `fraud_hold` | All customer-facing writes → `DENY`, including with human approval |
| `closed` | All customer-facing writes → `DENY` |

`fraud_hold` is deliberately absolute. If an account is under fraud review, the
support path is not where that gets overridden.

## Identity verification

Required for `reset_password` and `revoke_sessions`. Identity is verified when
the ticket's requester address matches the address on the customer record, and
the ticket is not hostile. A name in the body or a claim of authority is not
identity.

Two independent protections:

1. Policy denies the action outright when identity is unverified.
2. The tool reads the destination address from the **customer record**, never
   from the ticket — so even a hypothetical policy bypass cannot redirect a
   reset link to an attacker's inbox.

## Monetary rules

Applied in order; a `DENY` short-circuits, holds accumulate:

| Rule | Outcome | Default |
|---|---|---|
| Amount not a positive int | `DENY` | — |
| Invoice missing | `DENY` | — |
| Invoice belongs to another account | `DENY` | — |
| Invoice not `paid` | `DENY` | — |
| Amount > refundable balance | `DENY` | — |
| Autonomous financial actions disabled | `APPROVAL_REQUIRED` | enabled |
| Invoice older than the refund window | `APPROVAL_REQUIRED` | 90 days |
| Amount above the per-action ceiling | `APPROVAL_REQUIRED` | $200.00 |
| Rolling 24h total would exceed the cap | `APPROVAL_REQUIRED` | $2,000.00 |

Both boundaries are inclusive and tested as such: exactly $200.00 and exactly
90 days old are allowed, because off-by-one on a limit is the kind of bug that
only shows up in production.

The daily cap counts **only succeeded** refunds — a denial does not consume
budget — and it is computed from `action_log`, so it holds across tickets,
processes and restarts.

The action layer adds a belt to the braces: a refund is capped at the actual
refundable balance before policy even sees it, so a customer asking for $150 on
a $99 invoice gets $99 rather than a denial.

## Prompt injection

Scored by [`core/injection.py`](../src/support_swarm/core/injection.py) across
nine weighted signal classes (instruction override, role hijack, system-prompt
spoofing, delimiter breaking, authority claims, policy-bypass language,
exfiltration attempts, tool coercion, urgency pressure).

| Score | Classification | Effect |
|---|---|---|
| ≥ 3 | suspicious | `SENSITIVE_WRITE` and `FINANCIAL` → `APPROVAL_REQUIRED` |
| ≥ 6 | hostile | All customer-facing writes → `DENY`; ticket escalated at high severity |

Reads and `INTERNAL` actions are unaffected — a human needs the facts, and needs
to be told.

Setting `SWARM_BLOCK_ACTIONS_ON_INJECTION=false` degrades hostile handling to
`APPROVAL_REQUIRED`, never to `ALLOW`. There is no configuration that lets a
hostile ticket act autonomously.

Note the layering: injection scoring is defence in depth. The actual boundary is
that authority never derives from ticket text — the policy engine does not read
it.

## Idempotency

Keys are `sha256(ticket_id | tool | canonical_json(params))`. Sorted-key JSON
means parameter ordering cannot mint a second key for one logical action.

* Replay is checked **before** policy evaluation. A completed action has already
  changed the world; re-evaluating would deny the retry for a limit its own first
  execution created.
* The clean key is reserved for the row that actually succeeded. Refusals and
  failures get a suffixed key so they are recorded without colliding on the
  unique index — and without making a future attempt look already-completed.
* Keys are passed downstream as `Idempotency-Key`, but the swarm does not rely on
  vendors honouring them: `action_log`'s unique index is the real guarantee.

## Quality assurance

The last gate before a customer sees anything. Deterministic checks run first:

* the action a given intent requires actually succeeded;
* nothing was denied, held, or failed downstream;
* the reply is non-empty and leaks no internal identifiers (`cus_…`, `inv_…`);
* a refund reply states the amount that was *really* refunded.

Only then is a model consulted, and only to **tighten**: it can escalate or
request a revision, never clear a deterministic finding. A model outage falls
back to the deterministic verdict rather than blocking the ticket.

## Human approval

`POST /v1/tickets/{id}/approve` (admin scope) replays the **whole pipeline** with
an approval token rather than firing the parked call in isolation. Facts are
re-read, guardrails re-run, QA re-applied. So an approval granted at 09:00 cannot
execute against 14:00 state that has since changed — and because the idempotency
key derives from the same inputs, the replay cannot double-charge.

Approval raises authority to the approver's level. It converts `APPROVAL_REQUIRED`
to `ALLOW`; it does **not** override a `DENY`.

## Changing a limit

Limits are environment variables, not code:

```bash
SWARM_REFUND_AUTO_APPROVE_CENTS=5000        # tighter: $50 ceiling
SWARM_REFUND_DAILY_TOTAL_CAP_CENTS=50000    # $500/day
SWARM_ALLOW_AUTONOMOUS_FINANCIAL_ACTIONS=false   # every refund needs a human
SWARM_REFUND_WINDOW_DAYS=30
```

Adding a new *rule* means editing `evaluate()` and adding a test. That friction
is intentional.
