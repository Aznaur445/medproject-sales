import httpx
import pytest

from app.core.config import get_settings
from app.services import updates
from tests.test_web_sales import csrf_of, login

OLD, NEW, NEWER = "a" * 40, "b" * 40, "c" * 40


def github(commits: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/Aznaur445/medproject-sales/commits"
        return httpx.Response(
            200,
            json=[
                {
                    "sha": sha,
                    "commit": {"message": f"change {sha[0]}\n\nbody", "committer": {"date": "2026-10-05T08:00:00Z"}},
                }
                for sha in commits
            ],
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def update_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "update_dir", str(tmp_path))
    (tmp_path / "current").write_text(OLD + "\n")
    return tmp_path


def test_newer_commits_and_request(update_dir):
    commits = updates.recent_commits(http=github([NEWER, NEW, OLD]))
    assert [c.title for c in commits] == ["change c", "change b", "change a"]
    newer = updates.newer_than_current(commits)
    assert [c.sha for c in newer] == [NEWER, NEW]
    with pytest.raises(updates.UpdateError):
        updates.request_update("d" * 40, commits)  # not in the repository
    updates.request_update(NEWER, commits)
    assert (update_dir / "request").read_text().strip() == NEWER
    assert updates.pending_request() == NEWER
    (update_dir / "current").write_text(NEWER)
    assert updates.pending_request() is None


async def test_updates_page_and_install(client, monkeypatch, update_dir):
    await login(client, monkeypatch)
    commits = updates.recent_commits(http=github([NEW, OLD]))
    monkeypatch.setattr(updates, "recent_commits", lambda: commits)
    (update_dir / "status.json").write_text('{"state": "ok", "sha": "' + OLD + '", "at": "2026-10-05 08:00 UTC"}')
    page = await client.get("/updates")
    assert page.status_code == 200 and "change b" in page.text and "Установить bbbbbbb" in page.text
    assert "успешно" in page.text
    csrf = await csrf_of(client, "/updates")
    await client.post("/updates/install", data={"csrf_token": csrf, "sha": NEW})  # no confirmation
    assert not (update_dir / "request").exists()
    await client.post("/updates/install", data={"csrf_token": csrf, "sha": NEW, "confirm": "1"})
    assert (update_dir / "request").read_text().strip() == NEW
    assert "ожидает установки" in (await client.get("/updates")).text
