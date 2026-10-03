"""Acceptance criterion 8: no e-mail leaves without the owner's explicit approval."""

import smtplib
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.db import sync_session
from app.models import Approval, Listing, Message
from app.models.enums import ListingStatus, MessageStatus
from app.services import mailer
from app.services import settings_store as ss
from app.services.proposals import ApproveOutcome, ProposalError, approve, payload_hash, reject, update_text
from tests.factories import WORK_TIME, make_listing, make_proposal


class FakeSMTP:
    sent: list = []
    fail_with: Exception | None = None

    def __init__(self, settings):
        pass

    def send_message(self, email):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        FakeSMTP.sent.append(email)

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSMTP.sent = []
    FakeSMTP.fail_with = None


def send(message_id, now=WORK_TIME):
    with sync_session() as db:
        return mailer.send_message(db, message_id, smtp_factory=FakeSMTP, now=now)


def do_approve(message_id, confirmed=True):
    with sync_session() as db:
        result = approve(db, message_id, user_id=None, channel="web", confirmed=confirmed)
        db.commit()
        return result


def status(message_id):
    with sync_session() as db:
        return db.get(Message, message_id).status


def test_draft_is_never_sent_without_approval():
    mid = make_proposal(make_listing())
    assert status(mid) == MessageStatus.PENDING_APPROVAL
    result = send(mid)
    assert result.outcome == mailer.SendOutcome.BLOCKED
    assert FakeSMTP.sent == []


def test_forced_status_without_approval_record_is_blocked():
    """Even if something flips the status to APPROVED directly, there is no approval row -> nothing is sent."""
    mid = make_proposal(make_listing())
    with sync_session() as db:
        db.get(Message, mid).status = MessageStatus.APPROVED
        db.commit()
    assert send(mid).outcome == mailer.SendOutcome.BLOCKED
    assert FakeSMTP.sent == []
    assert status(mid) == MessageStatus.PENDING_APPROVAL


def test_approved_message_is_sent_once_with_attachment_and_unsubscribe():
    mid = make_proposal(make_listing())
    assert do_approve(mid).outcome == ApproveOutcome.APPROVED
    assert send(mid).outcome == mailer.SendOutcome.SENT
    assert send(mid).outcome == mailer.SendOutcome.ALREADY_SENT  # idempotent
    assert len(FakeSMTP.sent) == 1
    email = FakeSMTP.sent[0]
    assert email["To"] == "zakupki@clinic-example.ru"
    assert "kp@project-med.test" in email["From"]
    assert email["Message-ID"].startswith("<mp-")
    assert "List-Unsubscribe" in email
    assert "СТОП" in email.get_body().get_content()
    assert [a.get_filename() for a in email.iter_attachments()][0].endswith(".docx")
    with sync_session() as db:
        assert db.get(Message, mid).status == MessageStatus.SENT
        listing = db.query(Listing).one()
        assert listing.status == ListingStatus.PROPOSAL_SENT


def test_edit_after_approval_requires_new_approval():
    mid = make_proposal(make_listing())
    do_approve(mid)
    with sync_session() as db:
        update_text(db, mid, body="Новый текст письма", user_id=None)
        db.commit()
    assert status(mid) == MessageStatus.PENDING_APPROVAL
    assert send(mid).outcome == mailer.SendOutcome.BLOCKED
    assert FakeSMTP.sent == []


def test_tampered_content_after_approval_is_blocked():
    mid = make_proposal(make_listing())
    do_approve(mid)
    with sync_session() as db:  # bypassing the service, e.g. a bug or a manual DB change
        db.get(Message, mid).to_addr = "someone-else@evil-example.ru"
        db.commit()
    assert send(mid).outcome == mailer.SendOutcome.BLOCKED
    assert FakeSMTP.sent == []


def test_large_amount_needs_double_confirmation():
    mid = make_proposal(make_listing(), price=Decimal("1500000"))
    first = do_approve(mid, confirmed=False)
    assert first.outcome == ApproveOutcome.NEEDS_CONFIRMATION
    assert status(mid) == MessageStatus.PENDING_APPROVAL
    with sync_session() as db:
        assert db.query(Approval).count() == 0
    assert do_approve(mid, confirmed=True).outcome == ApproveOutcome.APPROVED
    with sync_session() as db:
        assert db.query(Approval).one().double_confirmed


def test_small_amount_single_click():
    mid = make_proposal(make_listing(), price=Decimal("500000"))
    assert do_approve(mid, confirmed=False).outcome == ApproveOutcome.APPROVED


def test_stale_view_cannot_be_approved():
    mid = make_proposal(make_listing())
    with sync_session() as db:
        old_hash = payload_hash(db.get(Message, mid))
        update_text(db, mid, body="Изменённый текст", user_id=None)
        db.commit()
    with sync_session() as db, pytest.raises(ProposalError):
        approve(db, mid, user_id=None, channel="bot", confirmed=True, expected_hash=old_hash)


def test_global_pause_defers():
    mid = make_proposal(make_listing())
    do_approve(mid)
    with sync_session() as db:
        ss.save_sync(db, ss.SendingRules(paused=True))
        db.commit()
    assert send(mid).outcome == mailer.SendOutcome.DEFERRED
    assert status(mid) == MessageStatus.QUEUED and FakeSMTP.sent == []


def test_optout_blocks_and_cancels():
    mid = make_proposal(make_listing())
    do_approve(mid)
    with sync_session() as db:
        mailer.add_optout(db, "clinic-example.ru", kind="domain", source="reply_stop")
        db.commit()
    assert status(mid) == MessageStatus.CANCELLED
    assert send(mid).outcome == mailer.SendOutcome.BLOCKED
    assert FakeSMTP.sent == []


def test_outside_window_and_weekend_deferred():
    mid = make_proposal(make_listing())
    do_approve(mid)
    assert send(mid, now=WORK_TIME.replace(hour=20)).outcome == mailer.SendOutcome.DEFERRED  # 23:00 MSK
    assert send(mid, now=WORK_TIME - timedelta(days=1)).outcome == mailer.SendOutcome.DEFERRED  # Sunday
    assert send(mid).outcome == mailer.SendOutcome.SENT


def test_daily_limit_with_warmup():
    with sync_session() as db:
        ss.save_sync(db, ss.SendingRules(warmup_start=2, warmup_step=1))
        db.commit()
    ids = [
        make_proposal(make_listing(customer=f"ООО «Клиника {i}»", email=f"z{i}@clinic{i}-example.ru")) for i in range(3)
    ]
    for mid in ids:
        do_approve(mid)
    outcomes = [send(mid).outcome for mid in ids]
    assert outcomes == [mailer.SendOutcome.SENT, mailer.SendOutcome.SENT, mailer.SendOutcome.DEFERRED]
    # next day warm-up allows one more
    assert send(ids[2], now=WORK_TIME + timedelta(days=1)).outcome == mailer.SendOutcome.SENT


def test_one_letter_per_organisation_per_interval():
    lid1 = make_listing(inn="7701234567")
    m1 = make_proposal(lid1)
    do_approve(m1)
    assert send(m1).outcome == mailer.SendOutcome.SENT
    lid2 = make_listing(inn="7701234567")  # same organisation, another request
    m2 = make_proposal(lid2)
    do_approve(m2)
    assert send(m2, now=WORK_TIME + timedelta(days=1)).outcome == mailer.SendOutcome.DEFERRED
    assert send(m2, now=WORK_TIME + timedelta(days=3)).outcome == mailer.SendOutcome.SENT


def test_crash_mid_send_is_not_resent():
    mid = make_proposal(make_listing())
    do_approve(mid)
    with sync_session() as db:
        db.get(Message, mid).status = MessageStatus.SENDING  # worker died after marking SENDING
        db.commit()
    assert send(mid).outcome == mailer.SendOutcome.FAILED
    assert FakeSMTP.sent == []


def test_connection_error_is_retried_but_disconnect_during_send_is_not():
    mid = make_proposal(make_listing())
    do_approve(mid)

    def broken(_settings):
        raise smtplib.SMTPConnectError(421, "busy")

    with sync_session() as db:
        assert mailer.send_message(db, mid, smtp_factory=broken, now=WORK_TIME).outcome == mailer.SendOutcome.DEFERRED
    FakeSMTP.fail_with = smtplib.SMTPServerDisconnected("gone")
    assert send(mid).outcome == mailer.SendOutcome.FAILED
    assert status(mid) == MessageStatus.FAILED


def test_mail_not_configured_defers(monkeypatch):
    mid = make_proposal(make_listing())
    do_approve(mid)
    monkeypatch.setattr(get_settings(), "mail_app_password", None)
    assert send(mid).outcome == mailer.SendOutcome.DEFERRED


def test_rejected_message_never_sent():
    mid = make_proposal(make_listing())
    with sync_session() as db:
        reject(db, mid, user_id=None, channel="bot", reason="не наш профиль")
        db.commit()
    assert send(mid).outcome == mailer.SendOutcome.BLOCKED
    with sync_session() as db:
        assert db.query(Listing).one().status == ListingStatus.REJECTED


def test_government_customer_excluded_and_no_proposal():
    lid = make_listing(customer="ГБУЗ «Городская больница № 1»")
    with sync_session() as db:
        listing = db.get(Listing, lid)
        assert listing.status == ListingStatus.EXCLUDED
        assert "госзаказчик" in listing.exclusion_reason
    with pytest.raises(ProposalError):
        make_proposal(lid)
