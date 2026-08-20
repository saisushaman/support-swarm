-- PostgreSQL schema for the support swarm.
--
-- SQLAlchemy's create_all() is used for dev and CI; this file is the reviewable
-- artefact for production, and adds the things an ORM will not: partial indexes
-- for the queue queries, a money-sanity CHECK, and the constraint the whole
-- idempotency guarantee rests on.

BEGIN;

CREATE TABLE IF NOT EXISTS customers (
    id          VARCHAR(32) PRIMARY KEY,
    email       VARCHAR(320) NOT NULL UNIQUE,
    full_name   VARCHAR(200) NOT NULL DEFAULT '',
    tier        VARCHAR(32)  NOT NULL DEFAULT 'standard',
    locale      VARCHAR(16)  NOT NULL DEFAULT 'en-US',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_customers_email ON customers (lower(email));

CREATE TABLE IF NOT EXISTS accounts (
    id          VARCHAR(32) PRIMARY KEY,
    customer_id VARCHAR(32) NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    status      VARCHAR(32) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'fraud_hold', 'closed')),
    plan        VARCHAR(32) NOT NULL DEFAULT 'free',
    mrr_cents   INTEGER     NOT NULL DEFAULT 0 CHECK (mrr_cents >= 0),
    seats       INTEGER     NOT NULL DEFAULT 1 CHECK (seats >= 0),
    region      VARCHAR(16) NOT NULL DEFAULT 'us',
    mfa_enabled BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_accounts_customer_id ON accounts (customer_id);

CREATE TABLE IF NOT EXISTS invoices (
    id             VARCHAR(32) PRIMARY KEY,
    account_id     VARCHAR(32) NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    amount_cents   INTEGER     NOT NULL CHECK (amount_cents >= 0),
    refunded_cents INTEGER     NOT NULL DEFAULT 0 CHECK (refunded_cents >= 0),
    currency       VARCHAR(3)  NOT NULL DEFAULT 'USD',
    status         VARCHAR(32) NOT NULL DEFAULT 'paid',
    description    VARCHAR(300) NOT NULL DEFAULT '',
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A refund can never exceed what was charged, whatever the caller believes.
    CONSTRAINT ck_invoice_refund_within_amount CHECK (refunded_cents <= amount_cents)
);
CREATE INDEX IF NOT EXISTS ix_invoices_account_issued ON invoices (account_id, issued_at DESC);

CREATE TABLE IF NOT EXISTS entitlements (
    id         VARCHAR(32) PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    key        VARCHAR(64) NOT NULL,
    enabled    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_entitlement_account_key UNIQUE (account_id, key)
);

CREATE TABLE IF NOT EXISTS tickets (
    id              VARCHAR(32) PRIMARY KEY,
    customer_id     VARCHAR(32) REFERENCES customers (id) ON DELETE SET NULL,
    requester_email VARCHAR(320) NOT NULL,
    subject         VARCHAR(300) NOT NULL DEFAULT '',
    body            TEXT         NOT NULL DEFAULT '',
    channel         VARCHAR(32)  NOT NULL DEFAULT 'email',
    status          VARCHAR(32)  NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'resolved', 'escalated',
                                      'awaiting_approval', 'failed')),
    priority        VARCHAR(16)  NOT NULL DEFAULT 'normal',
    intent          VARCHAR(64),
    resolution      TEXT,
    injection_score INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets (status);
CREATE INDEX IF NOT EXISTS ix_tickets_requester ON tickets (requester_email);
-- The queue view an operator actually opens.
CREATE INDEX IF NOT EXISTS ix_tickets_open_states ON tickets (updated_at DESC)
    WHERE status IN ('open', 'awaiting_approval', 'escalated');

-- Append-only. Nothing in the application updates or deletes from here.
CREATE TABLE IF NOT EXISTS ticket_events (
    id         VARCHAR(32) PRIMARY KEY,
    ticket_id  VARCHAR(32) NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    seq        INTEGER     NOT NULL,
    actor      VARCHAR(48) NOT NULL,
    kind       VARCHAR(32) NOT NULL,
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_ticket_event_seq UNIQUE (ticket_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_ticket_events_ticket_seq ON ticket_events (ticket_id, seq);

-- One row per ATTEMPTED mutation, including refusals. The unique index on
-- idempotency_key is what makes replay detection a database guarantee rather
-- than an application convention.
CREATE TABLE IF NOT EXISTS action_log (
    id              VARCHAR(32) PRIMARY KEY,
    ticket_id       VARCHAR(32) REFERENCES tickets (id) ON DELETE SET NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    tool            VARCHAR(64)  NOT NULL,
    actor           VARCHAR(48)  NOT NULL,
    risk            VARCHAR(24)  NOT NULL,
    status          VARCHAR(24)  NOT NULL
                    CHECK (status IN ('succeeded', 'failed', 'denied',
                                      'approval_required')),
    amount_cents    INTEGER      NOT NULL DEFAULT 0,
    request         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    result          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    decision        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    approved_by     VARCHAR(120),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_action_idempotency_key UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_action_log_ticket ON action_log (ticket_id);
-- Serves the rolling 24h refund cap without scanning the table.
CREATE INDEX IF NOT EXISTS ix_action_log_refund_window
    ON action_log (created_at DESC)
    WHERE tool = 'issue_refund' AND status = 'succeeded';

CREATE TABLE IF NOT EXISTS escalations (
    id          VARCHAR(32) PRIMARY KEY,
    ticket_id   VARCHAR(32) NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    queue       VARCHAR(64) NOT NULL,
    severity    VARCHAR(16) NOT NULL DEFAULT 'normal',
    reason_code VARCHAR(64) NOT NULL DEFAULT 'unresolved',
    summary_md  TEXT        NOT NULL DEFAULT '',
    diagnostics JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status      VARCHAR(24) NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'claimed', 'resolved')),
    assignee    VARCHAR(120),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_escalations_ticket ON escalations (ticket_id);
-- The on-call queue: open work, worst first.
CREATE INDEX IF NOT EXISTS ix_escalations_open
    ON escalations (severity, created_at DESC)
    WHERE status = 'open';

COMMIT;
