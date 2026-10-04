import pyotp
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import sync_session
from app.core.security import decrypt_secret
from app.models import User
from app.services import runtime_config
from app.services import settings_store as ss
from tests.conftest import CSRF_RE, get_csrf
from tests.test_web_sales import csrf_of, login


@pytest.fixture
def setup_token(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "setup_token", SecretStr("one-time-token-123"))
    return "one-time-token-123"


async def test_setup_creates_owner_once(client, setup_token):
    assert (await client.get("/setup?token=wrong")).status_code == 303
    page = await client.get(f"/setup?token={setup_token}")
    assert "Первый запуск" in page.text
    csrf = CSRF_RE.search(page.text).group(1)
    resp = await client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "token": setup_token,
            "username": "Owner",
            "password": "long-password-123",
            "password2": "long-password-123",
        },
    )
    assert resp.headers["location"] == "/login/2fa/setup"
    setup = await client.get("/login/2fa/setup")
    with sync_session() as db:
        user = db.execute(select(User)).scalar_one()
        assert user.username == "owner" and user.role == "owner"
        secret = decrypt_secret(user.totp_secret_encrypted)
    csrf = CSRF_RE.search(setup.text).group(1)
    resp = await client.post("/login/2fa/setup", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    assert resp.headers["location"] == "/"
    # The link no longer works once a user exists.
    client.cookies.clear()
    assert (await client.get(f"/setup?token={setup_token}")).status_code == 303
    csrf = await get_csrf(client)
    resp = await client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "token": setup_token,
            "username": "evil",
            "password": "long-password-123",
            "password2": "long-password-123",
        },
    )
    assert resp.status_code == 303
    with sync_session() as db:
        assert db.query(User).count() == 1


async def test_setup_disabled_without_token(client):
    assert (await client.get("/setup?token=")).status_code == 303


async def test_setup_validates_password(client, setup_token):
    page = await client.get(f"/setup?token={setup_token}")
    csrf = CSRF_RE.search(page.text).group(1)
    resp = await client.post(
        "/setup",
        data={"csrf_token": csrf, "token": setup_token, "username": "owner", "password": "short", "password2": "short"},
    )
    assert resp.status_code == 422


async def test_integrations_saved_encrypted_and_used(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_user", None)
    monkeypatch.setattr(get_settings(), "mail_app_password", None)
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/settings")
    resp = await client.post(
        "/settings/integrations",
        data={
            "csrf_token": csrf,
            "telegram_bot_token": "123456789:" + "A" * 35,
            "telegram_owner_ids": "111, 222",
            "mail_user": "KP@Project-Med.ru",
            "mail_app_password": "app-pass-xyz",
            "mail_from_name": "МедПроект",
        },
    )
    assert resp.status_code == 303
    with sync_session() as db:
        data = ss.load_sync(db, ss.Integrations)
    assert "app-pass-xyz" not in data.mail_app_password_enc
    runtime_config.reset_cache()
    tg = runtime_config.telegram_config()
    assert tg.ready and tg.owner_ids == (111, 222)
    mail = runtime_config.mail_config()
    assert mail.user == "kp@project-med.ru" and mail.password == "app-pass-xyz" and mail.ready
    # Empty secret fields keep stored values.
    await client.post(
        "/settings/integrations",
        data={"csrf_token": csrf, "telegram_owner_ids": "111", "mail_user": "kp@project-med.ru"},
    )
    runtime_config.reset_cache()
    assert runtime_config.mail_config().password == "app-pass-xyz"
    assert runtime_config.telegram_config().owner_ids == (111,)
    page = await client.get("/settings")
    assert "app-pass-xyz" not in page.text and "задан" in page.text


async def test_test_mail_reports_failure(client, monkeypatch):
    await login(client, monkeypatch)

    def boom(_settings):
        raise OSError("no route")

    monkeypatch.setattr("app.services.mailer.default_smtp", boom)
    csrf = await csrf_of(client, "/settings")
    await client.post("/settings/integrations/test-mail", data={"csrf_token": csrf})
    assert "Не удалось войти" in (await client.get("/settings")).text
