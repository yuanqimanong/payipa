"""requests.url_hash + per-batch unique index (multi-wave crawl dedup)

Revision ID: c1d2e3f4a5b6
Revises: b6f53fd80f94
Create Date: 2026-07-07 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b6f53fd80f94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()


def upgrade_pyp() -> None:
    # URL 指纹列 + 批内唯一索引：多波爬行时主控对 discovered 链接去重（ON CONFLICT DO NOTHING 靠此索引推断）。
    # url_hash 可空——历史行 / 无指纹行为 NULL，PG 视多个 NULL 互异，故不冲突。
    op.add_column("requests", sa.Column("url_hash", sa.String(length=64), nullable=True))
    op.create_index("uq_requests_batch_url_hash", "requests", ["batch_id", "url_hash"], unique=True)


def downgrade_pyp() -> None:
    op.drop_index("uq_requests_batch_url_hash", table_name="requests")
    op.drop_column("requests", "url_hash")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
