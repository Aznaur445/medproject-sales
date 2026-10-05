"""sources every 20 minutes

The owner asked for listings to be searched every 20 minutes: automatic sources that run less often are moved
to 20 minutes. Internet search spends only a few queries per run (rotation + daily limit in the connector).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05 10:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE source SET schedule_minutes = 20 "
        "WHERE connector IN ('web_search', 'web_page', 'telegram_public', 'email_alerts') AND schedule_minutes > 20"
    )


def downgrade() -> None:
    op.execute("UPDATE source SET schedule_minutes = 720 WHERE connector = 'web_search' AND schedule_minutes = 20")
