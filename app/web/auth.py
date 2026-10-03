"""Session-based auth: password + mandatory TOTP, CSRF protection, roles."""

import secrets
import time

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.models import User
from app.models.enums import UserRole

SESSION_USER = "uid"
SESSION_VERSION = "sv"
SESSION_TS = "ts"
SESSION_PRE_AUTH = "pre_uid"
SESSION_PRE_TS = "pre_ts"
SESSION_CSRF = "csrf"
PRE_AUTH_TTL_SECONDS = 300


class LoginRequired(Exception):
    """Raised when the request needs an authenticated user; handled with a redirect to /login."""


def client_ip(request: Request) -> str:
    # Caddy sets X-Forwarded-For; uvicorn runs with --proxy-headers and trusts only the proxy network.
    return request.client.host if request.client else "unknown"


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    expected = request.session.get(SESSION_CSRF)
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not expected or not sent or not secrets.compare_digest(expected, sent):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token invalid")


def start_pre_auth(request: Request, user: User) -> None:
    request.session.clear()
    request.session[SESSION_PRE_AUTH] = user.id
    request.session[SESSION_PRE_TS] = int(time.time())


def pre_auth_user_id(request: Request) -> int | None:
    uid = request.session.get(SESSION_PRE_AUTH)
    ts = request.session.get(SESSION_PRE_TS, 0)
    if uid is None or time.time() - ts > PRE_AUTH_TTL_SECONDS:
        return None
    return int(uid)


def complete_login(request: Request, user: User) -> None:
    request.session.clear()  # new session after privilege change (prevents fixation)
    request.session[SESSION_USER] = user.id
    request.session[SESSION_VERSION] = user.session_version
    request.session[SESSION_TS] = int(time.time())
    csrf_token(request)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    uid = request.session.get(SESSION_USER)
    if uid is None:
        raise LoginRequired
    max_age = get_settings().session_max_age_hours * 3600
    if time.time() - request.session.get(SESSION_TS, 0) > max_age:
        request.session.clear()
        raise LoginRequired
    user = await db.get(User, int(uid))
    if user is None or not user.is_active or user.session_version != request.session.get(SESSION_VERSION):
        request.session.clear()
        raise LoginRequired
    return user


async def require_owner(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
    return user
