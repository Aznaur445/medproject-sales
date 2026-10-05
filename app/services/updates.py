"""Owner-approved updates without SSH (spec: no automatic updates).

The panel lists newer commits of the GitHub repository; the owner picks one and the API writes its SHA to
UPDATE_DIR/request. The host timer (deploy/updater.sh) downloads that exact commit, rebuilds the stack, checks
health and rolls back on failure, reporting progress in UPDATE_DIR/status.json and UPDATE_DIR/current.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.core.config import get_settings

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class UpdateError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    message: str
    date: datetime | None

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def title(self) -> str:
        return self.message.splitlines()[0] if self.message else ""


def _dir() -> Path:
    return Path(get_settings().update_dir)


def _read(name: str) -> str | None:
    try:
        return (_dir() / name).read_text().strip() or None
    except OSError:
        return None


def current_version() -> str | None:
    value = _read("current")
    return value if value and SHA_RE.match(value) else None


def pending_request() -> str | None:
    value = _read("request")
    return value if value and SHA_RE.match(value) and value != current_version() else None


def status() -> dict:
    raw = _read("status.json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def updater_installed() -> bool:
    return _dir().is_dir() and current_version() is not None


def recent_commits(http: httpx.Client | None = None, limit: int = 15) -> list[Commit]:
    s = get_settings()
    client = http or httpx.Client(timeout=20)
    try:
        resp = client.get(
            f"https://api.github.com/repos/{s.update_repo}/commits",
            params={"sha": s.update_branch, "per_page": limit},
            headers={"Accept": "application/vnd.github+json", "User-Agent": "medproject-updater"},
        )
        if resp.status_code != 200:
            raise UpdateError(f"GitHub ответил HTTP {resp.status_code}")
        commits = []
        for item in resp.json():
            date = (item.get("commit") or {}).get("committer", {}).get("date")
            commits.append(
                Commit(
                    sha=item["sha"],
                    message=(item.get("commit") or {}).get("message", ""),
                    date=datetime.fromisoformat(date.replace("Z", "+00:00")) if date else None,
                )
            )
        return commits
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise UpdateError(f"Не удалось получить список версий: {type(exc).__name__}") from exc
    finally:
        if http is None:
            client.close()


def newer_than_current(commits: list[Commit]) -> list[Commit]:
    """Commits after the installed one (all of them when the installed one is unknown or not on the branch)."""
    current = current_version()
    for index, commit in enumerate(commits):
        if commit.sha == current:
            return commits[:index]
    return commits


def request_update(sha: str, allowed: list[Commit]) -> None:
    if not SHA_RE.match(sha) or sha not in {c.sha for c in allowed}:
        raise UpdateError("Эта версия не найдена в репозитории")
    if not _dir().is_dir():
        raise UpdateError("Обновление из панели не настроено на сервере")
    tmp = _dir() / "request.tmp"  # replace atomically: the file may belong to root after a host-side write
    tmp.write_text(sha + "\n")
    tmp.replace(_dir() / "request")
