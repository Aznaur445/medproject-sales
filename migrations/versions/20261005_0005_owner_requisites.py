"""owner requisites from the sample proposal

Single-owner service: the owner asked to take the requisites from the proposal they sent (МедПроект,
ИП Алибеков Э. М.). Only empty fields are filled, values already entered in the panel are kept.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05 14:10:00+00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUISITES = {
    "brand": "МедПроект",
    "tagline": "Экспертное проектирование медицинских пространств",
    "legal_name": "ИП Алибеков Эмир Муратович",
    "short_name": "ИП Алибеков Э. М.",
    "inn": "231119468748",
    "ogrnip": "324237500355774",
    "phone": "8 (918) 263-36-27",
    "email": "med-project@bk.ru",
    "website": "project-med.ru",
    "okved_note": (
        "В соответствии с выпиской из ЕГРИП основным/дополнительным видом деятельности Исполнителя являются "
        "коды ОКВЭД 71.11 (деятельность в области архитектуры) и 71.12, что даёт полное законное право "
        "на выполнение данного комплекса работ."
    ),
    "gip_note": (
        "Ответственным за проект назначается главный инженер проектов (ГИП) Лев Николаевич Никитин "
        "(Национальный реестр специалистов НОПРИЗ, опыт работы ГИПом более 20 лет)."
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT value FROM setting WHERE key = 'requisites'")).first()
    current = dict(row[0]) if row and row[0] else {}
    merged = {**current, **{k: v for k, v in REQUISITES.items() if not str(current.get(k) or "").strip()}}
    if row is None:
        conn.execute(
            sa.text("INSERT INTO setting (key, value) VALUES ('requisites', CAST(:v AS jsonb))"),
            {"v": json.dumps(merged, ensure_ascii=False)},
        )
    elif merged != current:
        conn.execute(
            sa.text("UPDATE setting SET value = CAST(:v AS jsonb), updated_at = now() WHERE key = 'requisites'"),
            {"v": json.dumps(merged, ensure_ascii=False)},
        )


def downgrade() -> None:
    pass  # owner data: never removed automatically
