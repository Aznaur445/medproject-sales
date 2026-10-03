import re
from decimal import Decimal as D

import pytest

from app.bot import approvals
from app.core.db import sync_session
from app.models import Message, ProposalVersion
from app.services.cards import card_keyboard, card_text
from app.services.proposals import ProposalError, prepare_proposal, version_evaluation, version_sections
from tests.factories import make_listing, make_proposal
from tests.test_proposal_doc import docx_text
from tests.test_web_sales import csrf_of, login


def latest_version(db, mid):
    return db.get(ProposalVersion, db.get(Message, mid).proposal_version_id)


def test_owner_sets_each_section_price_removes_and_adds():
    lid = make_listing()
    mid = make_proposal(lid)
    with sync_session() as db:
        message = prepare_proposal(
            db, lid, render_pdf=False, section_prices={"AR": D("300000"), "SS": D("0"), "TX": D("150000")}
        )
        db.commit()
        version = latest_version(db, message.id)
        sections = {s["code"]: D(s["price"]) for s in version_sections(version)}
    assert message.id == mid  # same draft e-mail, new version
    assert "SS" not in sections and sections["AR"] == D("300000") and sections["TX"] == D("150000")
    assert version.price == sum(sections.values())
    assert version.version == 2


def test_margin_changes_with_price_and_removed_sections_lower_cost():
    lid = make_listing()
    mid = make_proposal(lid)
    with sync_session() as db:
        base = version_evaluation(db, latest_version(db, mid))
        prepare_proposal(db, lid, render_pdf=False, section_prices={"OVIK": D("0")})
        db.commit()
        reduced = version_evaluation(db, latest_version(db, mid))
    assert reduced.direct_cost < base.direct_cost
    assert reduced.price < base.price


def test_unknown_section_rejected():
    lid = make_listing()
    make_proposal(lid)
    with sync_session() as db, pytest.raises(ProposalError):
        prepare_proposal(db, lid, render_pdf=False, section_prices={"NOPE": D("1")})


def test_card_lists_sections_margin_and_button():
    mid = make_proposal(make_listing(), price=D("1200000"))
    with sync_session() as db:
        message = db.get(Message, mid)
        text = card_text(db, message)
        buttons = [b["text"] for row in card_keyboard(db, message)["inline_keyboard"] for b in row]
    assert "АР:" in text and "ОВиК:" in text
    assert "При этой сумме: прибыль" in text
    assert "🧮 Цены по разделам" in buttons


def test_bot_sections_info_lists_current_and_addable():
    mid = make_proposal(make_listing())
    info = approvals._sections_info(mid)
    assert "AR — Архитектурные решения (АР):" in info
    assert "Итого:" in info and "Можно добавить" in info and "TX —" in info


async def test_web_section_prices_form(client, monkeypatch):
    import app.worker.tasks_sales as tasks

    monkeypatch.setattr(tasks.send_card_task, "delay", lambda *a: None)
    await login(client, monkeypatch)
    lid = make_listing()
    csrf = await csrf_of(client, f"/listings/{lid}")
    await client.post(
        f"/listings/{lid}/estimate",
        data={"csrf_token": csrf, "area_m2": "800", "sections": ["AR", "EOM", "VK", "OVIK", "SS"]},
    )
    resp = await client.post(
        f"/listings/{lid}/proposal",
        data={
            "csrf_token": csrf,
            "mode": "sections",
            "sp_AR": "500 000",
            "sp_EOM": "400000",
            "sp_VK": "0",
            "sp_OVIK": "600000",
            "sp_SS": "300000",
            "sp_PB": "250 000",
            "sp_TX": "",
        },
    )
    assert resp.status_code == 303
    page = await client.get(f"/listings/{lid}")
    assert "2 050 000 ₽" in page.text
    assert "Текущая версия: прибыль" in page.text
    docx = re.search(r'href="(/files/[^"]+\.docx)"', page.text).group(1)
    text = docx_text((await client.get(docx)).content)
    assert "пожарной безопасности" in text and "Водоснабжение" not in text
