"""Daily backups of PostgreSQL and files: local copy + S3 (Timeweb Cloud), retention N days.

Usage:
    python -m app.ops.backup run              # make a backup now
    python -m app.ops.backup list             # list local and remote backups
    python -m app.ops.backup restore <stamp>  # restore DB and files from a backup (DESTRUCTIVE)
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)
STAMP_FORMAT = "%Y%m%d-%H%M%S"
S3_PREFIX = "backups/"


def _pg_env(settings: Settings) -> tuple[list[str], dict[str, str]]:
    """Connection args for pg_dump/pg_restore; password goes via env, never via argv."""
    url = urlsplit(settings.database_url.replace("+psycopg", ""))
    args = [
        "-h",
        url.hostname or "localhost",
        "-p",
        str(url.port or 5432),
        "-U",
        unquote(url.username or ""),
        "-d",
        url.path.lstrip("/"),
    ]
    env = {**os.environ, "PGPASSWORD": unquote(url.password or "")}
    return args, env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _s3(settings: Settings):
    if not (settings.s3_endpoint_url and settings.s3_bucket and settings.s3_access_key and settings.s3_secret_key):
        return None
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
    )


def run_backup(settings: Settings | None = None, now: datetime | None = None) -> Path:
    settings = settings or get_settings()
    stamp = (now or datetime.now(UTC)).strftime(STAMP_FORMAT)
    target = Path(settings.backup_dir) / stamp
    target.mkdir(parents=True, exist_ok=False)
    try:
        conn_args, env = _pg_env(settings)
        dump = target / "db.dump"
        subprocess.run(
            ["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-f", str(dump), *conn_args],
            env=env,
            check=True,
            capture_output=True,
            timeout=3600,
        )
        files = target / "files.tar.gz"
        files_dir = Path(settings.files_dir)
        with tarfile.open(files, "w:gz") as tar:
            if files_dir.exists():
                tar.add(files_dir, arcname="files")
        manifest = {
            "stamp": stamp,
            "created_at": datetime.now(UTC).isoformat(),
            "files": {p.name: {"sha256": _sha256(p), "size": p.stat().st_size} for p in (dump, files)},
        }
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    client = _s3(settings)
    if client is not None:
        for path in sorted(target.iterdir()):
            client.upload_file(str(path), settings.s3_bucket, f"{S3_PREFIX}{stamp}/{path.name}")
    prune(settings)
    log.info("backup_done", stamp=stamp, uploaded=client is not None)
    return target


def _parse_stamp(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def prune(settings: Settings, now: datetime | None = None) -> list[str]:
    """Delete backups older than retention (local and S3). Always keeps the newest one."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.backup_retention_days)
    removed: list[str] = []
    root = Path(settings.backup_dir)
    local = sorted(p for p in root.iterdir() if p.is_dir() and _parse_stamp(p.name)) if root.exists() else []
    for path in local[:-1]:
        if _parse_stamp(path.name) < cutoff:
            shutil.rmtree(path)
            removed.append(path.name)
    client = _s3(settings)
    if client is not None:
        stamps = sorted({k.split("/")[1] for k in _s3_keys(client, settings) if k.count("/") >= 2})
        for stamp in stamps[:-1]:
            parsed = _parse_stamp(stamp)
            if parsed and parsed < cutoff:
                for key in _s3_keys(client, settings, f"{S3_PREFIX}{stamp}/"):
                    client.delete_object(Bucket=settings.s3_bucket, Key=key)
                removed.append(f"s3:{stamp}")
    return removed


def _s3_keys(client, settings: Settings, prefix: str = S3_PREFIX) -> list[str]:
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        keys += [obj["Key"] for obj in page.get("Contents", [])]
    return keys


def list_backups(settings: Settings | None = None) -> dict[str, list[str]]:
    settings = settings or get_settings()
    root = Path(settings.backup_dir)
    local = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
    client = _s3(settings)
    remote = sorted({k.split("/")[1] for k in _s3_keys(client, settings)}) if client else []
    return {"local": local, "s3": remote}


def restore(stamp: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    source = Path(settings.backup_dir) / stamp
    if not source.exists():
        client = _s3(settings)
        if client is None:
            raise FileNotFoundError(f"Backup {stamp} not found locally and S3 is not configured")
        source.mkdir(parents=True)
        for name in ("db.dump", "files.tar.gz", "manifest.json"):
            client.download_file(settings.s3_bucket, f"{S3_PREFIX}{stamp}/{name}", str(source / name))
    manifest = json.loads((source / "manifest.json").read_text())
    for name, meta in manifest["files"].items():
        if _sha256(source / name) != meta["sha256"]:
            raise ValueError(f"Checksum mismatch for {name}: backup is corrupted")
    conn_args, env = _pg_env(settings)
    subprocess.run(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--exit-on-error",
            *conn_args,
            str(source / "db.dump"),
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=3600,
    )
    _restore_files(source / "files.tar.gz", Path(settings.files_dir))
    log.info("restore_done", stamp=stamp)


def _restore_files(archive: Path, files_dir: Path) -> None:
    """Replace the contents of files_dir (it may be a Docker volume mount point, so keep the dir itself)."""
    files_dir.mkdir(parents=True, exist_ok=True)
    staging = files_dir.parent / f".restore-{int(time.time())}"
    staging.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        for child in files_dir.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        restored = staging / "files"
        if restored.exists():
            for child in restored.iterdir():
                shutil.move(str(child), files_dir / child.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "dev")
    parser = argparse.ArgumentParser(prog="app.ops.backup")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("stamp")
    r.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    args = parser.parse_args(argv)
    if args.command == "run":
        started = time.monotonic()
        path = run_backup(settings)
        print(f"Бэкап готов: {path} ({time.monotonic() - started:.0f} с)")
    elif args.command == "list":
        print(json.dumps(list_backups(settings), indent=2, ensure_ascii=False))
    else:
        if not args.yes:
            answer = input(f"Текущие данные будут ЗАМЕНЕНЫ данными из бэкапа {args.stamp}. Введите ДА: ")
            if answer.strip().upper() != "ДА":
                print("Отменено.")
                return 1
        restore(args.stamp, settings)
        print("Восстановление завершено. Перезапустите сервисы: docker compose restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
