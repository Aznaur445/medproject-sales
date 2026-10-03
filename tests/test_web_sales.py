import argparse
import re

import pyotp
import pytest
from sqlalchemy import select

from app.cli import create_user
from app.core.db import sync_session
from app.models import Approval, Estimate, Listing, Message, OptOut, PriceTable
from app.models.enums import ListingStatus, MessageStatus
from tests.conftest import CSRF_RE, get_csrf
from tests.test_auth import PASSWORD, totp_secret
from tests.test_proposal_doc import docx_text


@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    calls = []
    import app.worker.tasks_sales as tasks

    monkeypatch.setattr(tasks.send_card_task, "delay", lambda *a: calls.append(("card", a)))
    monkeypatch.setattr(tasks.send_message_task, "delay", lambda *a: calls.append(("send", a)))
    return calls


async def login(client, monkeypatch, username="owner", role="owner"):
    monkeypatch.setattr("app.cli._read_password", lambda _g: PASSWORD)
    create_user(argparse.Namespace(username=username, role=role, telegram_id=None, generate=False))
    csrf = await get_csrf(client)
    await client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": csrf})
    setup = await client.get("/login/2fa/setup")
    csrf = CSRF_RE.search(setup.text).group(1)
    code = pyotp.TOTP(totp_secret(username)).now()
    resp = await client.post("/login/2fa/setup", data={"code": code, "csrf_token": csrf})
    assert resp.status_code == 303


async def csrf_of(client, url="/"):
    return CSRF_RE.search((await client.get(url)).text).group(1)


async def create_listing(client, **extra):
    csrf = await csrf_of(client, "/listings/new")
    data = {
        "csrf_token": csrf,
        "title": "Медцентр на Ленина",
        "customer_name": "ООО «Клиника»",
        "customer_inn": "7701234567",
        "area_m2": "650",
        "object_type": "Медицинский центр / клиника",
        "contact_email": "tender@clinic-example.ru",
        "contact_source": "https://clinic-example.ru/tenders",
    }
    data.update(extra)
    resp = await client.post("/listings/new", data=data)
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[-1])


async def test_full_flow_listing_estimate_proposal_approve(client, monkeypatch, _no_celery):
    await login(client, monkeypatch)
    lid = await create_listing(client)
    page = await client.get(f"/listings/{lid}")
    assert "Расчёт стоимости" in page.text and "пример" in page.text

    csrf = CSRF_RE.search(page.text).group(1)
    resp = await client.post(
        f"/listings/{lid}/estimate",
        data={
            "csrf_token": csrf,
            "area_m2": "650",
            "object_type": "Медицинский центр / клиника",
            "trips": "1",
            "sections": ["AR", "EOM", "OVIK", "VK", "SS", "TX"],
            "modifiers": ["reconstruction"],
        },
    )
    assert resp.status_code == 303
    page = await client.get(f"/listings/{lid}")
    assert "Рекомендуемая" in page.text

    check = await client.get(f"/listings/{lid}/price-check", params={"price": "1 200 000"})
    assert "маржа" in check.text

    resp = await client.post(f"/listings/{lid}/proposal", data={"csrf_token": csrf, "price": "1 400 000"})
    assert resp.status_code == 303
    assert ("card", (1,)) in _no_celery
    page = await client.get(f"/listings/{lid}")
    assert "Будет отправлено" in page.text and "tender@clinic-example.ru" in page.text
    assert "Подтверждаю сумму" in page.text  # 1.4M >= 1M -> double confirmation
    expected_hash = re.search(r'name="expected_hash" value="([0-9a-f]+)"', page.text).group(1)

    # Without the confirmation checkbox nothing is approved.
    resp = await client.post("/messages/1/approve", data={"csrf_token": csrf, "expected_hash": expected_hash})
    assert resp.status_code == 303
    with sync_session() as db:
        assert db.get(Message, 1).status == MessageStatus.PENDING_APPROVAL
        assert db.query(Approval).count() == 0
    assert not [c for c in _no_celery if c[0] == "send"]

    resp = await client.post(
        "/messages/1/approve", data={"csrf_token": csrf, "expected_hash": expected_hash, "confirm": "1"}
    )
    with sync_session() as db:
        assert db.get(Message, 1).status == MessageStatus.APPROVED
        approval = db.query(Approval).one()
        assert approval.double_confirmed and approval.channel == "web"
    assert ("send", (1,)) in _no_celery

    # Proposal document is downloadable and contains no internal numbers.
    page = await client.get(f"/listings/{lid}")
    docx_link = re.search(r'href="(/files/[^"]+\.docx)"', page.text).group(1)
    doc = await client.get(docx_link)
    assert doc.status_code == 200 and doc.content[:2] == b"PK"
    text = docx_text(doc.content).lower()
    assert "1\u00a0400\u00a0000" in text
    for internal in ("себестоим", "маржа", "прибыль", "гип "):
        assert internal not in text
    with sync_session() as db:
        estimate = db.query(Estimate).one()
        assert f"{int(estimate.cost_total):,}".replace(",", "\u00a0") not in text


async def test_edit_resets_approval_flow(client, monkeypatch):
    await login(client, monkeypatch)
    lid = await create_listing(client)
    csrf = await csrf_of(client, f"/listings/{lid}")
    await client.post(f"/listings/{lid}/estimate", data={"csrf_token": csrf, "area_m2": "300", "sections": ["AR"]})
    await client.post(f"/listings/{lid}/proposal", data={"csrf_token": csrf, "price": "300000"})
    page = await client.get(f"/listings/{lid}")
    old_hash = re.search(r'name="expected_hash" value="([0-9a-f]+)"', page.text).group(1)
    await client.post(
        "/messages/1/edit",
        data={
            "csrf_token": csrf,
            "to_addr": "tender@clinic-example.ru",
            "subject": "КП",
            "body": "Новый текст письма для заказчика",
        },
    )
    resp = await client.post("/messages/1/approve", data={"csrf_token": csrf, "expected_hash": old_hash})
    assert resp.status_code == 303
    with sync_session() as db:
        assert db.get(Message, 1).status == MessageStatus.PENDING_APPROVAL  # stale view rejected


async def test_viewer_cannot_approve_or_create(client, monkeypatch):
    await login(client, monkeypatch, username="viewer", role="viewer")
    csrf = await csrf_of(client)
    resp = await client.post("/listings/new", data={"csrf_token": csrf, "title": "x", "customer_name": "y"})
    assert resp.status_code == 403
    resp = await client.post("/messages/1/approve", data={"csrf_token": csrf})
    assert resp.status_code == 403
    assert (await client.get("/listings")).status_code == 200


async def test_government_listing_is_excluded(client, monkeypatch):
    await login(client, monkeypatch)
    lid = await create_listing(client, customer_name="ГБУЗ «Областная больница»", customer_inn="")
    with sync_session() as db:
        assert db.get(Listing, lid).status == ListingStatus.EXCLUDED
    page = await client.get("/listings")
    assert "ГБУЗ" not in page.text  # excluded listings are hidden from the default list
    page = await client.get("/listings?status=excluded")
    assert "госзаказчик" in page.text


async def test_contact_email_requires_source(client, monkeypatch):
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/listings/new")
    resp = await client.post(
        "/listings/new",
        data={"csrf_token": csrf, "title": "Объект", "customer_name": "ООО «А»", "contact_email": "a@a-example.ru"},
    )
    assert resp.status_code == 422 and "опубликован" in resp.text


async def test_prices_save_creates_new_version(client, monkeypatch):
    await login(client, monkeypatch)
    page = await client.get("/prices")
    csrf = CSRF_RE.search(page.text).group(1)
    data = {
        "csrf_token": csrf,
        "s0_code": "AR",
        "s0_name": "АР",
        "s0_stage": "rd",
        "s0_rate": "200",
        "s0_min": "100000",
        "s0_cost": "45",
        "s0_default": "1",
        "gip_share": "10",
        "other_costs_share": "3",
        "tax_rate": "7",
        "target_margin": "30",
        "min_margin": "15",
        "max_uplift": "15",
        "trip_cost": "20000",
        "object_types": "Клиника = 1,0\nСтоматология = 0,9",
        "modifiers": "urgent | Срочно | 1,2",
        "area_steps": "0 = 1\n",
        "regions": "",
    }
    resp = await client.post("/prices", data=data)
    assert resp.status_code == 303
    with sync_session() as db:
        active = db.execute(select(PriceTable).where(PriceTable.is_active.is_(True))).scalar_one()
        assert active.version == 2 and active.data["sections"][0]["cost_share"] == "0.4500"
        assert not active.data["is_example"]


async def test_settings_pause_and_optout(client, monkeypatch):
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/settings")
    await client.post("/settings/pause", data={"csrf_token": csrf, "paused": "1"})
    assert "отправка на паузе" in (await client.get("/")).text
    await client.post("/optouts", data={"csrf_token": csrf, "value": "Spam@Clinic-Example.ru"})
    with sync_session() as db:
        assert db.query(OptOut).one().value == "spam@clinic-example.ru"


async def test_file_download_rejects_traversal(client, monkeypatch):
    await login(client, monkeypatch)
    assert (await client.get("/files/../../etc/passwd")).status_code == 404
    assert (await client.get("/files/%2e%2e/%2e%2e/etc/passwd")).status_code == 404


async def test_files_require_login(client):
    assert (await client.get("/files/proposals/1/v1/x.docx")).status_code == 303
