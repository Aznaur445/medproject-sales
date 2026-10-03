"""Listings: manual creation, organisations, contacts, estimates."""

import re
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContactChannel, Estimate, Listing, Organization, PriceTable, Source
from app.models.enums import ChannelKind, ListingStatus, OrgType, SourceKind
from app.services.audit import audit_sync
from app.services.calculator import EstimateInput, EstimateResult, PriceTableData, calculate
from app.services.gov_filter import government_reason
from app.services.price_examples import EXAMPLE_PRICE_TABLE

MANUAL_SOURCE = "Ручное добавление"


class ManualListingInput(BaseModel):
    title: str = Field(min_length=3, max_length=1000)
    customer_name: str = Field(min_length=2, max_length=500)
    customer_inn: str | None = None
    region: str | None = None
    address: str | None = None
    object_type: str | None = None
    area_m2: Decimal | None = None
    description: str | None = None
    url: str | None = None
    deadline_at: datetime | None = None
    contact_email: EmailStr | None = None
    contact_source: str | None = None  # where the e-mail was published (152-FZ: keep the source)

    @field_validator("customer_inn")
    @classmethod
    def _inn(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = re.sub(r"\D", "", v)
        if len(v) not in (10, 12):
            raise ValueError("ИНН должен содержать 10 или 12 цифр")
        return v

    @field_validator("contact_source")
    @classmethod
    def _source(cls, v: str | None, info) -> str | None:
        if info.data.get("contact_email") and not (v or "").strip():
            raise ValueError("Укажите, где опубликован e-mail (ссылка или описание)")
        return v


def manual_source(db: Session) -> Source:
    source = db.execute(select(Source).where(Source.name == MANUAL_SOURCE)).scalar_one_or_none()
    if source is None:
        source = Source(name=MANUAL_SOURCE, kind=SourceKind.MANUAL, connector="manual", enabled=True)
        db.add(source)
        db.flush()
    return source


def get_or_create_org(db: Session, name: str, inn: str | None) -> Organization:
    org = None
    if inn:
        org = db.execute(select(Organization).where(Organization.inn == inn)).scalar_one_or_none()
    if org is None:
        org = db.execute(
            select(Organization).where(Organization.name == name.strip(), Organization.inn.is_(None))
        ).scalar_one_or_none()
    if org is None:
        org = Organization(name=name.strip(), inn=inn, tags=[])
        db.add(org)
        db.flush()
    elif inn and not org.inn:
        org.inn = inn
    reason = government_reason(org.name)
    org.org_type = OrgType.GOVERNMENT if reason else (org.org_type or OrgType.UNKNOWN)
    return org


def add_email_channel(db: Session, org: Organization, email: str, source: str) -> ContactChannel:
    email = email.strip().lower()
    channel = db.execute(
        select(ContactChannel).where(
            ContactChannel.organization_id == org.id,
            ContactChannel.kind == ChannelKind.EMAIL,
            ContactChannel.value == email,
        )
    ).scalar_one_or_none()
    if channel is None:
        channel = ContactChannel(
            organization_id=org.id,
            kind=ChannelKind.EMAIL,
            value=email,
            is_public_for_orders=True,
            source_url=source[:1000],
            collected_at=datetime.now(UTC),
            priority=10,
        )
        db.add(channel)
        db.flush()
    return channel


def create_manual_listing(db: Session, data: ManualListingInput, user_id: int | None, actor: str = "web") -> Listing:
    org = get_or_create_org(db, data.customer_name, data.customer_inn)
    if data.contact_email:
        add_email_channel(db, org, str(data.contact_email), data.contact_source or "")
    listing = Listing(
        source_id=manual_source(db).id,
        url=data.url,
        title=data.title.strip(),
        description=data.description,
        organization_id=org.id,
        region=data.region,
        address=data.address,
        object_type=data.object_type,
        area_m2=data.area_m2,
        deadline_at=data.deadline_at,
        status=ListingStatus.FOUND,
        budget_unknown=True,
        tags=[],
        score_explanation={},
        extracted={},
    )
    reason = government_reason(data.customer_name, data.title, data.description, data.url)
    if reason:
        listing.status = ListingStatus.EXCLUDED
        listing.exclusion_reason = reason
    db.add(listing)
    db.flush()
    audit_sync(
        db,
        "listing_created",
        actor=actor,
        user_id=user_id,
        entity_type="listing",
        entity_id=listing.id,
        details={"excluded": reason},
    )
    return listing


def listing_email(db: Session, listing: Listing) -> str | None:
    if listing.organization_id is None:
        return None
    return db.execute(
        select(ContactChannel.value)
        .where(
            ContactChannel.organization_id == listing.organization_id,
            ContactChannel.kind == ChannelKind.EMAIL,
            ContactChannel.is_public_for_orders.is_(True),
        )
        .order_by(ContactChannel.priority, ContactChannel.id)
        .limit(1)
    ).scalar_one_or_none()


def active_price_table(db: Session) -> PriceTable:
    table = db.execute(
        select(PriceTable).where(PriceTable.is_active.is_(True)).order_by(PriceTable.version.desc()).limit(1)
    ).scalar_one_or_none()
    if table is not None and table.data.get("is_example") and not table.data.get("package_curve"):
        # Older example without the area curve: replace with the current example as a new version.
        table.is_active = False
        upgraded = PriceTable(
            name=table.name,
            version=table.version + 1,
            is_active=True,
            data=PriceTableData.model_validate(EXAMPLE_PRICE_TABLE).model_dump(mode="json"),
            comment="Пример обновлён: цена пакета зависит от площади.",
        )
        db.add(upgraded)
        db.flush()
        return upgraded
    if table is None:
        table = PriceTable(
            name="Основной прайс",
            version=1,
            is_active=True,
            data=PriceTableData.model_validate(EXAMPLE_PRICE_TABLE).model_dump(mode="json"),
            comment="Пример, восстановленный по КП для ООО «Медскан». Замените своими ставками.",
        )
        db.add(table)
        db.flush()
    return table


def save_price_table(db: Session, data: PriceTableData, user_id: int | None, comment: str = "") -> PriceTable:
    """Every edit is a new version; old versions stay for estimates that reference them."""
    current = active_price_table(db)
    current.is_active = False
    new = PriceTable(
        name=current.name,
        version=current.version + 1,
        is_active=True,
        data=data.model_dump(mode="json"),
        comment=comment,
    )
    db.add(new)
    db.flush()
    audit_sync(db, "price_table_saved", actor="web", user_id=user_id, entity_type="price_table", entity_id=new.id)
    return new


def make_estimate(db: Session, listing: Listing, inputs: EstimateInput) -> tuple[Estimate, EstimateResult]:
    table = active_price_table(db)
    result = calculate(PriceTableData.model_validate(table.data), inputs)
    estimate = Estimate(
        listing_id=listing.id,
        price_table_id=table.id,
        inputs=inputs.model_dump(mode="json"),
        breakdown=result.model_dump(mode="json"),
        recommended_price=result.recommended_price,
        min_price=result.min_price,
        cost_total=result.direct_cost,
        scenarios={k: v.model_dump(mode="json") for k, v in result.scenarios.items()},
    )
    db.add(estimate)
    if listing.area_m2 is None:
        listing.area_m2 = inputs.area_m2
    if inputs.object_type and not listing.object_type:
        listing.object_type = inputs.object_type
    db.flush()
    return estimate, result


def latest_estimate(db: Session, listing_id: int) -> Estimate | None:
    return db.execute(
        select(Estimate).where(Estimate.listing_id == listing_id).order_by(Estimate.id.desc()).limit(1)
    ).scalar_one_or_none()
