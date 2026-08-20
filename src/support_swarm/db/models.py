"""Relational model.

Portable across PostgreSQL (production) and SQLite (tests / laptop): no
dialect-specific column types, ``JSON`` rather than ``JSONB``, string ids
rather than native uuid.

The append-only tables -- ``ticket_events`` and ``action_log`` -- are the audit
spine. Nothing in the swarm mutates them after insert, so a full replay of what
each agent saw and did is always reconstructible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cus"))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    tier: Mapped[str] = mapped_column(String(32), default="standard")
    locale: Mapped[str] = mapped_column(String(16), default="en-US")

    accounts: Mapped[list[Account]] = relationship(back_populates="customer")


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("acct"))
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="active")
    plan: Mapped[str] = mapped_column(String(32), default="free")
    mrr_cents: Mapped[int] = mapped_column(Integer, default=0)
    seats: Mapped[int] = mapped_column(Integer, default=1)
    region: Mapped[str] = mapped_column(String(16), default="us")
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    customer: Mapped[Customer] = relationship(back_populates="accounts")


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("inv"))
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(32), default="paid")
    description: Mapped[str] = mapped_column(String(300), default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Entitlement(Base, TimestampMixin):
    __tablename__ = "entitlements"
    __table_args__ = (UniqueConstraint("account_id", "key", name="uq_entitlement_account_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ent"))
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tkt"))
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requester_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String(32), default="email")
    # open | resolved | escalated | awaiting_approval | failed
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    injection_score: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    events: Mapped[list[TicketEvent]] = relationship(
        back_populates="ticket", order_by="TicketEvent.seq"
    )


class TicketEvent(Base, TimestampMixin):
    """Append-only transcript of everything the swarm saw, decided and said."""

    __tablename__ = "ticket_events"
    __table_args__ = (
        UniqueConstraint("ticket_id", "seq", name="uq_ticket_event_seq"),
        Index("ix_ticket_events_ticket_seq", "ticket_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("evt"))
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(48), nullable=False)
    # customer_message | agent_thought | tool_call | policy_decision | reply | escalation
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    ticket: Mapped[Ticket] = relationship(back_populates="events")


class ActionLog(Base, TimestampMixin):
    """One row per attempted mutation. The unique key gives us idempotency."""

    __tablename__ = "action_log"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_action_idempotency_key"),
        Index("ix_action_log_tool_created", "tool", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("act"))
    ticket_id: Mapped[str | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(48), nullable=False)
    risk: Mapped[str] = mapped_column(String(24), nullable=False)
    # allowed | denied | approval_required | succeeded | failed
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Escalation(Base, TimestampMixin):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("esc"))
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="normal")
    reason_code: Mapped[str] = mapped_column(String(64), default="unresolved")
    summary_md: Mapped[str] = mapped_column(Text, default="")
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    # open | claimed | resolved
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    assignee: Mapped[str | None] = mapped_column(String(120), nullable=True)
