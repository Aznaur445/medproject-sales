from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ProposalStatus


class CaseStudy(TimestampMixin, Base):
    __tablename__ = "case_study"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    object_type: Mapped[str | None] = mapped_column(String(100))
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    year: Mapped[int | None] = mapped_column(Integer)
    customer_name: Mapped[str | None] = mapped_column(String(500))
    region: Mapped[str | None] = mapped_column(String(200))
    stages: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    sections: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    result: Mapped[str | None] = mapped_column(Text)
    review_text: Mapped[str | None] = mapped_column(Text)
    links: Mapped[list[Any]] = mapped_column(default=list)
    photos: Mapped[list[Any]] = mapped_column(default=list)
    can_mention_customer: Mapped[bool] = mapped_column(Boolean, default=False)


class PriceTable(TimestampMixin, Base):
    """Versioned price list: rates per m2 by object type/stage/section, coefficients, cost rates."""

    __tablename__ = "price_table"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)  # validated by pydantic schema in services
    comment: Mapped[str | None] = mapped_column(Text)


class Estimate(Base):
    __tablename__ = "estimate"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"), index=True)
    price_table_id: Mapped[int | None] = mapped_column(ForeignKey("price_table.id", ondelete="SET NULL"))
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    breakdown: Mapped[dict[str, Any]] = mapped_column(default=dict)
    recommended_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    min_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cost_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    scenarios: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Proposal(TimestampMixin, Base):
    __tablename__ = "proposal"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"), index=True)
    estimate_id: Mapped[int | None] = mapped_column(ForeignKey("estimate.id", ondelete="SET NULL"))
    template_name: Mapped[str] = mapped_column(String(200), default="default")
    status: Mapped[ProposalStatus] = mapped_column(String(32), default=ProposalStatus.DRAFT)
    current_version: Mapped[int] = mapped_column(Integer, default=0)

    versions: Mapped[list["ProposalVersion"]] = relationship(order_by="ProposalVersion.version")


class ProposalVersion(Base):
    __tablename__ = "proposal_version"
    __table_args__ = (UniqueConstraint("proposal_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    content: Mapped[dict[str, Any]] = mapped_column(default=dict)  # template variables
    cover_letter: Mapped[str | None] = mapped_column(Text)
    docx_key: Mapped[str | None] = mapped_column(String(500))
    pdf_key: Mapped[str | None] = mapped_column(String(500))
    estimate_id: Mapped[int | None] = mapped_column(ForeignKey("estimate.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoredDocument(TimestampMixin, Base):
    """Owner's document vault (SRO, extracts, references, CVs) with validity dates."""

    __tablename__ = "stored_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    doc_type: Mapped[str] = mapped_column(String(100), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    issued_on: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)


class ParticipationPackage(TimestampMixin, Base):
    __tablename__ = "participation_package"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    requirements: Mapped[list[Any]] = mapped_column(default=list)
    missing_documents: Mapped[list[Any]] = mapped_column(default=list)
    instructions_md: Mapped[str | None] = mapped_column(Text)
    checklist: Mapped[list[Any]] = mapped_column(default=list)
    zip_key: Mapped[str | None] = mapped_column(String(500))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(TimestampMixin, Base):
    __tablename__ = "task"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), default="todo")  # call / followup / todo
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    call_script: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarEvent(TimestampMixin, Base):
    __tablename__ = "calendar_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(32))  # deadline / call / meeting / doc_expiry / reminder
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"))
    stored_document_id: Mapped[int | None] = mapped_column(ForeignKey("stored_document.id", ondelete="CASCADE"))
    remind_before_minutes: Mapped[list[Any]] = mapped_column(default=list)
    reminders_sent: Mapped[list[Any]] = mapped_column(default=list)


class Approval(Base):
    """Immutable record: who approved/rejected what, when, and exactly which payload."""

    __tablename__ = "approval"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(BigInteger)
    decision: Mapped[str] = mapped_column(String(32))  # ApprovalDecision
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16))  # web / bot
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    double_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
