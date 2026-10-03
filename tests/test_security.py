import pyotp

from app.core.logging import mask_text, redact_processor
from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    new_totp_secret,
    verify_password,
    verify_totp,
)


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")
    assert not verify_password("not-a-hash", "x")


def test_secret_encryption_roundtrip():
    token = encrypt_secret("app-password-123")
    assert "app-password-123" not in token
    assert decrypt_secret(token) == "app-password-123"


def test_totp_verification():
    secret = new_totp_secret()
    assert verify_totp(secret, pyotp.TOTP(secret).now())
    assert not verify_totp(secret, "000000") or pyotp.TOTP(secret).now() == "000000"
    assert not verify_totp(secret, "abc")


def test_logs_redact_secret_keys_and_personal_data():
    event = redact_processor(
        None,
        "info",
        {
            "event": "mail to ivanov@clinic.ru, phone +7 (918) 263-36-27",
            "password": "hunter2",
            "mail_app_password": "x",
            "api_key": "sk-1",
            "code": "123456",
            "status_code": 200,
            "nested": {"token": "abc", "note": "ok"},
        },
    )
    assert event["password"] == "***"
    assert event["mail_app_password"] == "***"
    assert event["api_key"] == "***"
    assert event["code"] == "***"
    assert event["status_code"] == 200
    assert event["nested"] == {"token": "***", "note": "ok"}
    assert "ivanov@" not in event["event"]
    assert "263-36" not in event["event"]


def test_mask_bot_token():
    assert "AAH" not in mask_text("url https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/x")
