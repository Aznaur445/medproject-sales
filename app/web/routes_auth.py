from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    new_totp_secret,
    password_needs_rehash,
    qr_svg_data_uri,
    totp_uri,
    verify_password,
    verify_totp,
)
from app.models import User
from app.services.audit import audit
from app.services.ratelimit import AttemptLimiter
from app.web.auth import (
    client_ip,
    complete_login,
    current_user,
    pre_auth_user_id,
    start_pre_auth,
    verify_csrf,
)
from app.web.templating import render

router = APIRouter(dependencies=[Depends(verify_csrf)])

# Dummy hash so that unknown usernames take the same time as wrong passwords.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _limiter() -> AttemptLimiter:
    s = get_settings()
    return AttemptLimiter(get_redis(), "login", s.login_max_attempts, s.login_lockout_minutes * 60)


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(
    request: Request, username: str = Form(...), password: str = Form(...), db: AsyncSession = Depends(get_db)
):
    ip = client_ip(request)
    username = username.strip().lower()
    limiter = _limiter()
    if await limiter.is_blocked(f"ip:{ip}", f"user:{username}"):
        await audit(db, "login_blocked", details={"username": username}, ip=ip)
        await db.commit()
        return render(request, "login.html", 429, error="Слишком много попыток. Попробуйте позже.")
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    valid = verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None or not valid or not user.is_active:
        await limiter.register_failure(f"ip:{ip}", f"user:{username}")
        await audit(db, "login_failed", user_id=user.id if user else None, details={"username": username}, ip=ip)
        await db.commit()
        return render(request, "login.html", 401, error="Неверный логин или пароль")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await db.commit()
    start_pre_auth(request, user)
    target = "/login/2fa" if user.totp_enabled else "/login/2fa/setup"
    return RedirectResponse(target, status_code=303)


async def _pre_auth_user(request: Request, db: AsyncSession) -> User | None:
    uid = pre_auth_user_id(request)
    if uid is None:
        return None
    user = await db.get(User, uid)
    return user if user and user.is_active else None


@router.get("/login/2fa/setup")
async def totp_setup_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _pre_auth_user(request, db)
    if user is None or user.totp_enabled:
        return RedirectResponse("/login", status_code=303)
    if not user.totp_secret_encrypted:
        user.totp_secret_encrypted = encrypt_secret(new_totp_secret())
        await db.commit()
    secret = decrypt_secret(user.totp_secret_encrypted)
    return render(request, "totp_setup.html", qr=qr_svg_data_uri(totp_uri(secret, user.username)), secret=secret)


@router.get("/login/2fa")
async def totp_form(request: Request, db: AsyncSession = Depends(get_db)):
    if await _pre_auth_user(request, db) is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "totp.html")


@router.post("/login/2fa")
@router.post("/login/2fa/setup")
async def totp_verify(request: Request, code: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _pre_auth_user(request, db)
    if user is None or not user.totp_secret_encrypted:
        return RedirectResponse("/login", status_code=303)
    ip = client_ip(request)
    limiter = _limiter()
    template = "totp.html" if user.totp_enabled else "totp_setup.html"
    if await limiter.is_blocked(f"totp:{user.id}"):
        return render(request, "login.html", 429, error="Слишком много попыток. Попробуйте позже.")
    secret = decrypt_secret(user.totp_secret_encrypted)
    code = code.strip().replace(" ", "")
    # Each code can be used once (replay protection within its validity window).
    fresh = await get_redis().set(f"totp_used:{user.id}:{code}", 1, nx=True, ex=120)
    if not verify_totp(secret, code) or not fresh:
        await limiter.register_failure(f"totp:{user.id}")
        await audit(db, "totp_failed", user_id=user.id, ip=ip)
        await db.commit()
        context = {}
        if not user.totp_enabled:
            context = {"qr": qr_svg_data_uri(totp_uri(secret, user.username)), "secret": secret}
        return render(request, template, 401, error="Неверный код", **context)
    if not user.totp_enabled:
        user.totp_enabled = True
        await audit(db, "totp_enabled", user_id=user.id, ip=ip)
    user.last_login_at = datetime.now(UTC)
    await limiter.reset(f"totp:{user.id}", f"user:{user.username}")
    await audit(db, "login_success", user_id=user.id, ip=ip)
    await db.commit()
    complete_login(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await audit(db, "logout", user_id=user.id, ip=client_ip(request))
    await db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.post("/logout-everywhere")
async def logout_everywhere(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.session_version += 1
    await audit(db, "logout_everywhere", user_id=user.id, ip=client_ip(request))
    await db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
