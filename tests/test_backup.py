from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import sync_session
from app.models import Setting
from app.ops import backup


@pytest.fixture
def backup_settings(tmp_path):
    settings = get_settings().model_copy(
        update={"backup_dir": str(tmp_path / "backups"), "files_dir": str(tmp_path / "files")}
    )
    (tmp_path / "files" / "proposals").mkdir(parents=True)
    (tmp_path / "files" / "proposals" / "kp.pdf").write_bytes(b"%PDF original")
    return settings


def test_backup_and_restore_roundtrip(backup_settings):
    with sync_session() as db:
        db.add(Setting(key="requisites", value={"inn": "231119468748"}))
        db.commit()

    path = backup.run_backup(backup_settings)
    assert {p.name for p in path.iterdir()} == {"db.dump", "files.tar.gz", "manifest.json"}

    # Damage the data after the backup.
    with sync_session() as db:
        db.get(Setting, "requisites").value = {"inn": "broken"}
        db.add(Setting(key="added_later", value={}))
        db.commit()
    files = Path(backup_settings.files_dir)
    (files / "proposals" / "kp.pdf").write_bytes(b"corrupted")
    (files / "junk.txt").write_text("junk")

    backup.restore(path.name, backup_settings)

    with sync_session() as db:
        keys = {s.key: s.value for s in db.execute(select(Setting)).scalars()}
    assert keys == {"requisites": {"inn": "231119468748"}}
    assert (files / "proposals" / "kp.pdf").read_bytes() == b"%PDF original"
    assert not (files / "junk.txt").exists()


def test_restore_refuses_corrupted_backup(backup_settings):
    path = backup.run_backup(backup_settings)
    (path / "db.dump").write_bytes(b"garbage")
    with pytest.raises(ValueError, match="Checksum"):
        backup.restore(path.name, backup_settings)


def test_prune_keeps_retention_window_and_newest(backup_settings):
    root = Path(backup_settings.backup_dir)
    now = datetime(2026, 10, 3, tzinfo=UTC)
    for days in (1, 13, 15, 30):
        (root / (now - timedelta(days=days)).strftime(backup.STAMP_FORMAT)).mkdir(parents=True)
    removed = backup.prune(backup_settings, now=now)
    assert len(removed) == 2
    assert len(list(root.iterdir())) == 2


def test_prune_never_deletes_the_only_backup(backup_settings):
    root = Path(backup_settings.backup_dir)
    old = datetime(2020, 1, 1, tzinfo=UTC).strftime(backup.STAMP_FORMAT)
    (root / old).mkdir(parents=True)
    assert backup.prune(backup_settings) == []
