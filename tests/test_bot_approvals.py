from decimal import Decimal

import pytest

from app.bot import approvals
from app.core.db import sync_session
from app.core.numbers import parse_amount
from app.models import Message
from app.models.enums import MessageStatus
from app.services.cards import card_keyboard, card_text, short_hash
from app.services.proposals import ProposalError, update_text
from tests.factories import make_listing, make_proposal


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1450000", Decimal("1450000")),
        ("1 450 000 ₽", Decimal("1450000")),
        ("1,45 млн", Decimal("1450000")),
        ("950к", Decimal("950000")),
        ("950 тыс", Decimal("950000")),
        ("1450000 руб.", Decimal("1450000")),
    ],
)
def test_parse_amount(text, value):
    assert parse_amount(text) == value


@pytest.mark.parametrize("text", ["abc", "-5", "0", "99999999999"])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_card_shows_what_where_and_from():
    mid = make_proposal(make_listing(), price=Decimal("1500000"))
    with sync_session() as db:
        message = db.get(Message, mid)
        text = card_text(db, message)
        keyboard = card_keyboard(db, message)
    assert "1 500 000 ₽" in text
    assert "zakupki@clinic-example.ru" in text and "kp@project-med.test" in text
    assert "маржа" in text  # owner-only card shows margin
    buttons = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
    assert {
        "✅ Согласовать",
        "✏️ Изменить сумму",
        "📝 Редактировать текст",
        "⏸ Отложить",
        "❌ Отклонить",
        "🌐 Открыть в панели",
    } <= set(buttons)


def test_bot_double_confirmation_and_stale_card():
    mid = make_proposal(make_listing(), price=Decimal("1500000"))
    with sync_session() as db:
        h = short_hash(db.get(Message, mid))
    assert approvals._approve(mid, h, tg_id=1, confirmed=False)[0] == "confirm"
    assert approvals._check_hash(mid, h) is None
    with sync_session() as db:
        update_text(db, mid, body="Другой текст письма заказчику", user_id=None)
        db.commit()
    assert approvals._check_hash(mid, h) == "stale"
    with pytest.raises(ProposalError):
        approvals._approve(mid, h, tg_id=1, confirmed=True)
    with sync_session() as db:
        new_h = short_hash(db.get(Message, mid))
    assert approvals._approve(mid, new_h, tg_id=1, confirmed=True)[0] == "approved"
    with sync_session() as db:
        assert db.get(Message, mid).status == MessageStatus.APPROVED


def test_pause_and_resume_from_bot():
    from app.services import settings_store as ss

    approvals._set_pause(True, tg_id=1)
    with sync_session() as db:
        assert ss.load_sync(db, ss.SendingRules).paused
    approvals._set_pause(False, tg_id=1)
    with sync_session() as db:
        assert not ss.load_sync(db, ss.SendingRules).paused


def test_add_from_bot_and_today_pipeline():
    listing_id, excluded = approvals._add_listing("https://example-board.ru/123 проект стоматологии", tg_id=1)
    assert excluded is None
    gov_id, gov_reason = approvals._add_listing("Закупка ГБУЗ поликлиника проектирование", tg_id=1)
    assert gov_reason
    assert "Найдены: 1" in approvals._pipeline() and "Исключены: 1" in approvals._pipeline()
    assert "На согласовании: 0" in approvals._today()
