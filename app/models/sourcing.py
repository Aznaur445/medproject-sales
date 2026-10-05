from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from app.models.enums import ListingStatus, OrgType, SourceKind, SourceLegalStatus


class Source(TimestampMixin, Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[SourceKind] = mapped_column(String(32))
    connector: Mapped[str] = mapped_column(String(100))  # plugin identifier
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    legal_status: Mapped[SourceLegalStatus] = mapped_column(String(32), default=SourceLegalStatus.ALLOWED)
    schedule_minutes: Mapped[int] = mapped_column(Integer, default=20)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class Organization(TimestampMixin, Base):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    inn: Mapped[str | None] = mapped_column(String(12), unique=True)
    kpp: Mapped[str | None] = mapped_column(String(9))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    org_type: Mapped[OrgType] = mapped_column(String(16), default=OrgType.UNKNOWN)
    website: Mapped[str | None] = mapped_column(String(500))
    region: Mapped[str | None] = mapped_column(String(200))
    is_clinic_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="organization")


class Contact(TimestampMixin, Base):
    """A person at an organization. Only work contacts published for orders/procurement."""

    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str | None] = mapped_column(String(300))
    position: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    source_note: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    organization: Mapped[Organization] = relationship(back_populates="contacts")
    channels: Mapped[list["ContactChannel"]] = relationship(back_populates="contact")


class ContactChannel(TimestampMixin, Base):
    __tablename__ = "contact_channel"
    __table_args__ = (UniqueConstraint("organization_id", "kind", "value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # ChannelKind
    value: Mapped[str] = mapped_column(String(500))
    is_public_for_orders: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    contact: Mapped[Contact | None] = relationship(back_populates="channels")


class Listing(TimestampMixin, Base):
    """A request/tender for design works found in some source."""

    __tablename__ = "listing"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"))
    region: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(1000))
    object_type: Mapped[str | None] = mapped_column(String(100))
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    budget: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    budget_unknown: Mapped[bool] = mapped_column(Boolean, default=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[ListingStatus] = mapped_column(String(32), default=ListingStatus.FOUND, index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(300))
    lost_reason: Mapped[str | None] = mapped_column(String(300))
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_explanation: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dedup_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("listing.id", ondelete="SET NULL"))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    complex_procedure: Mapped[bool] = mapped_column(Boolean, default=False)
    extracted: Mapped[dict[str, Any]] = mapped_column(default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)

    organization: Mapped[Organization | None] = relationship()
    versions: Mapped[list["ListingVersion"]] = relationship(back_populates="listing", order_by="ListingVersion.version")
    documents: Mapped[list["ListingDocument"]] = relationship(back_populates="listing")


class ListingVersion(Base):
    __tablename__ = "listing_version"
    __table_args__ = (UniqueConstraint("listing_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    diff: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    listing: Mapped[Listing] = relationship(back_populates="versions")


class ListingDocument(Base):
    __tablename__ = "listing_document"
    __table_args__ = (UniqueConstraint("listing_id", "sha256"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(2000))
    storage_key: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    text_extracted: Mapped[bool] = mapped_column(Boolean, default=False)
    parsed: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    listing: Mapped[Listing] = relationship(back_populates="documents")
