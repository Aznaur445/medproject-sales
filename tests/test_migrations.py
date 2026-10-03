from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.core.db import get_sync_engine
from app.models import Base
from tests.conftest import alembic_config


def test_models_match_migrations():
    with get_sync_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_again():
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")
