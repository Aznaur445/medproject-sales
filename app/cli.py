"""Admin commands: python -m app.cli <command>."""

import argparse
import getpass
import secrets
import sys

from sqlalchemy import select

from app.core.db import sync_session
from app.core.security import hash_password
from app.models import User
from app.models.enums import UserRole
from app.services.audit import audit_sync

MIN_PASSWORD_LENGTH = 12


def _read_password(generate: bool) -> str:
    if generate:
        password = secrets.token_urlsafe(15)
        print(f"Сгенерированный пароль (сохраните в менеджер паролей): {password}")
        return password
    while True:
        password = getpass.getpass("Пароль (не меньше 12 символов): ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print("Слишком короткий пароль.")
            continue
        if getpass.getpass("Повторите пароль: ") != password:
            print("Пароли не совпадают.")
            continue
        return password


def create_user(args: argparse.Namespace) -> int:
    username = args.username.strip().lower()
    with sync_session() as db:
        if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
            print(f"Пользователь {username} уже существует.")
            return 1
        user = User(
            username=username,
            password_hash=hash_password(_read_password(args.generate)),
            role=UserRole(args.role),
            telegram_id=args.telegram_id,
        )
        db.add(user)
        db.flush()
        audit_sync(db, "user_created", actor="cli", entity_type="user", entity_id=user.id)
        db.commit()
    print(f"Готово. Откройте панель, войдите как {username} и настройте 2FA по QR-коду.")
    return 0


def _get_user(db, username: str) -> User | None:
    user = db.execute(select(User).where(User.username == username.strip().lower())).scalar_one_or_none()
    if user is None:
        print("Пользователь не найден.")
    return user


def set_password(args: argparse.Namespace) -> int:
    with sync_session() as db:
        user = _get_user(db, args.username)
        if user is None:
            return 1
        user.password_hash = hash_password(_read_password(args.generate))
        user.session_version += 1
        audit_sync(db, "password_reset", actor="cli", entity_type="user", entity_id=user.id)
        db.commit()
    print("Пароль изменён, все сессии завершены.")
    return 0


def reset_2fa(args: argparse.Namespace) -> int:
    with sync_session() as db:
        user = _get_user(db, args.username)
        if user is None:
            return 1
        user.totp_enabled = False
        user.totp_secret_encrypted = None
        user.session_version += 1
        audit_sync(db, "totp_reset", actor="cli", entity_type="user", entity_id=user.id)
        db.commit()
    print("2FA сброшена. При следующем входе будет показан новый QR-код.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="Создать пользователя")
    p.add_argument("username")
    p.add_argument("--role", choices=[r.value for r in UserRole], default=UserRole.OWNER.value)
    p.add_argument("--telegram-id", type=int)
    p.add_argument("--generate", action="store_true", help="Сгенерировать пароль")
    p.set_defaults(func=create_user)

    p = sub.add_parser("set-password", help="Сменить пароль")
    p.add_argument("username")
    p.add_argument("--generate", action="store_true")
    p.set_defaults(func=set_password)

    p = sub.add_parser("reset-2fa", help="Сбросить 2FA (потерян телефон)")
    p.add_argument("username")
    p.set_defaults(func=reset_2fa)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
