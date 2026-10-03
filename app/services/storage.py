"""File storage on the local volume (FILES_DIR). Keys are relative POSIX paths."""

import hashlib
import re
from pathlib import Path

from app.core.config import get_settings

SAFE_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._-]+")


def root() -> Path:
    path = Path(get_settings().files_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str) -> str:
    cleaned = SAFE_RE.sub("_", name).strip("._")
    return cleaned[:150] or "file"


def path_for(key: str) -> Path:
    base = root().resolve()
    path = (base / key).resolve()
    if base not in path.parents:
        raise ValueError("Invalid storage key")
    return path


def save_bytes(key: str, data: bytes) -> str:
    path = path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic: readers never see half-written files
    return key


def read_bytes(key: str) -> bytes:
    return path_for(key).read_bytes()


def sha256_of(key: str) -> str:
    return hashlib.sha256(read_bytes(key)).hexdigest()
