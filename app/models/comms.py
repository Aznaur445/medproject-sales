from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import MessageStatus


class Thread(TimestampMixin, Base):
    __tablename__ = "thread"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listing.id", ondelete="SET NULL"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default="open")  # open / escalated / closed

    messages: Mapped[list["Message"]] = relationship(back_populates="thread", order_by="Message.id")


class Message(TimestampMixin, Base):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("thread.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # MessageDirection
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[MessageStatus] = mapped_column(String(32), index=True)
    from_addr: Mapped[str | None] = mapped_column(String(500))
    to_addr: Mapped[str | None] = mapped_column(String(500))
    subject: Mapped[str | None] = mapped_column(String(1000))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list[Any]] = mapped_column(default=list)
    email_message_id: Mapped[str | None] = mapped_column(String(500), unique=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    references: Mapped[str | None] = mapped_column(Text)
    # Unique key making the send operation idempotent across retries.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approval.id", ondelete="SET NULL"))
    sequence_step_id: Mapped[int | None] = mapped_column(ForeignKey("sequence_step.id", ondelete="SET NULL"))
    proposal_version_id: Mapped[int | None] = mapped_column(ForeignKey("proposal_version.id", ondelete="SET NULL"))
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"), index=True)
    classification: Mapped[str | None] = mapped_column(String(32))
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    send_attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    thread: Mapped[Thread] = relationship(back_populates="messages")


class Sequence(TimestampMixin, Base):
    __tablename__ = "sequence"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    steps: Mapped[list["SequenceStep"]] = relationship(order_by="SequenceStep.position")


class SequenceStep(Base):
    __tablename__ = "sequence_step"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(ForeignKey("sequence.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    day_offset: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))  # send_email / other_channel / remind_call / close
    channel: Mapped[str | None] = mapped_column(String(16))
    prompt_key: Mapped[str | None] = mapped_column(String(100))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)


class OptOut(Base):
    """Do-not-contact registry. Checked before every outbound message."""

    __tablename__ = "optout"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(500), unique=True)  # normalized e-mail / phone / domain
    kind: Mapped[str] = mapped_column(String(16))  # email / phone / domain / organization
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))  # reply_stop / link / manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
