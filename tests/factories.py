from datetime import UTC, datetime
from decimal import Decimal

from app.core.db import sync_session
from app.services.calculator import EstimateInput
from app.services.listings import ManualListingInput, create_manual_listing, make_estimate
from app.services.proposals import prepare_proposal

# Monday 2026-10-05 11:00 Moscow time: inside the default sending window.
WORK_TIME = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


def make_listing(customer="ООО «Клиника Здоровье»", email="zakupki@clinic-example.ru", area="800", **extra) -> int:
    with sync_session() as db:
        data = ManualListingInput(
            title="Реконструкция медицинского центра",
            customer_name=customer,
            customer_inn=extra.pop("inn", None),
            area_m2=Decimal(area),
            contact_email=email,
            contact_source="https://clinic-example.ru/zakupki" if email else None,
            address="г. Казань, ул. Тестовая, 1",
            **extra,
        )
        listing = create_manual_listing(db, data, user_id=None)
        db.commit()
        return listing.id


def make_proposal(listing_id: int, price: Decimal | None = None) -> int:
    with sync_session() as db:
        listing = db.get(__import__("app.models", fromlist=["Listing"]).Listing, listing_id)
        make_estimate(db, listing, EstimateInput(area_m2=listing.area_m2 or Decimal("800")))
        message = prepare_proposal(db, listing_id, price=price, render_pdf=False)
        db.commit()
        return message.id
