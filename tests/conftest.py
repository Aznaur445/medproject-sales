import os
import re

os.environ.update(
    ENV="test",
    SECRET_KEY="test-secret-key-" + "x" * 48,
    FERNET_KEY="ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=",
    DATABASE_URL=os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg://medproject:medproject@localhost:5432/medproject_test"
    ),
    REDIS_URL=os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_OWNER_IDS="",
)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import get_sync_engine  # noqa: E402
from app.core.redis import get_sync_redis  # noqa: E402
from app.models import Base  # noqa: E402


def alembic_config() -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = get_settings().database_url
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _migrated_db():
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_sync_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    get_sync_redis().flushdb()
    yield


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


async def get_csrf(client: AsyncClient, url: str = "/login") -> str:
    resp = await client.get(url)
    match = CSRF_RE.search(resp.text)
    assert match, resp.text
    return match.group(1)
