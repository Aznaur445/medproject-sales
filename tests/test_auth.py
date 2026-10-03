import argparse

import pyotp
from sqlalchemy import select

from app.cli import create_user
from app.core.db import sync_session
from app.core.security import decrypt_secret
from app.models import AuditLog, User
from tests.conftest import CSRF_RE, get_csrf

PASSWORD = "very-strong-password-1"


def make_user(monkeypatch, username="owner") -> None:
    monkeypatch.setattr("app.cli._read_password", lambda _generate: PASSWORD)
    assert create_user(argparse.Namespace(username=username, role="owner", telegram_id=None, generate=False)) == 0


def totp_secret(username="owner") -> str:
    with sync_session() as db:
        user = db.execute(select(User).where(User.username == username)).scalar_one()
        return decrypt_secret(user.totp_secret_encrypted)


async def password_step(client, password=PASSWORD):
    csrf = await get_csrf(client)
    return await client.post("/login", data={"username": "Owner", "password": password, "csrf_token": csrf})


async def full_login(client) -> str:
    resp = await password_step(client)
    assert resp.headers["location"] == "/login/2fa/setup"
    setup = await client.get("/login/2fa/setup")
    assert "data:image/svg+xml" in setup.text
    csrf = CSRF_RE.search(setup.text).group(1)
    code = pyotp.TOTP(totp_secret()).now()
    resp = await client.post("/login/2fa/setup", data={"code": code, "csrf_token": csrf})
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    return code


async def test_dashboard_requires_login(client):
    resp = await client.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


async def test_full_login_with_totp_setup(client, monkeypatch):
    make_user(monkeypatch)
    await full_login(client)
    resp = await client.get("/")
    assert resp.status_code == 200 and "Сводка" in resp.text
    with sync_session() as db:
        assert db.execute(select(User)).scalar_one().totp_enabled
        actions = [a.action for a in db.execute(select(AuditLog)).scalars()]
    assert "totp_enabled" in actions and "login_success" in actions


async def test_password_alone_does_not_grant_access(client, monkeypatch):
    make_user(monkeypatch)
    await password_step(client)
    assert (await client.get("/")).status_code == 303


async def test_second_login_asks_for_code_and_rejects_replay(client, monkeypatch):
    make_user(monkeypatch)
    used = await full_login(client)
    client.cookies.clear()
    resp = await password_step(client)
    assert resp.headers["location"] == "/login/2fa"
    csrf = await get_csrf(client, "/login/2fa")
    # The code used during setup is still inside its validity window, but must not work twice.
    resp = await client.post("/login/2fa", data={"code": used, "csrf_token": csrf})
    assert resp.status_code == 401
    assert (await client.get("/")).status_code == 303


async def test_wrong_password_and_lockout(client, monkeypatch):
    make_user(monkeypatch)
    for _ in range(5):
        resp = await password_step(client, "wrong")
        assert resp.status_code == 401
    resp = await password_step(client)  # even the right password is blocked now
    assert resp.status_code == 429


async def test_post_without_csrf_is_rejected(client, monkeypatch):
    make_user(monkeypatch)
    resp = await client.post("/login", data={"username": "owner", "password": PASSWORD})
    assert resp.status_code == 403


async def test_logout_everywhere_invalidates_session(client, monkeypatch):
    make_user(monkeypatch)
    await full_login(client)
    page = await client.get("/")
    csrf = CSRF_RE.search(page.text).group(1)
    stolen_cookie = client.cookies.get("mp_session")
    resp = await client.post("/logout-everywhere", data={"csrf_token": csrf})
    assert resp.status_code == 303
    client.cookies.set("mp_session", stolen_cookie)
    assert (await client.get("/")).status_code == 303


async def test_security_headers(client):
    resp = await client.get("/login")
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
