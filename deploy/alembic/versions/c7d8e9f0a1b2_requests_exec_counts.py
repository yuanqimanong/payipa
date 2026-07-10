"""requests exec counts: persist per-request parse counts + duration for monitor (M5)

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()


def upgrade_pyp() -> None:
    # 每请求解析计数（agent ExecSummary 回报）+ 耗时，供 core.monitor 聚合数据质量/时延。
    # nullable：历史行无计数（聚合时按 NULL 跳过），新完成的请求由 handle_result 回填。
    op.add_column("requests", sa.Column("count_ok", sa.Integer(), nullable=True))
    op.add_column("requests", sa.Column("count_fail", sa.Integer(), nullable=True))
    op.add_column("requests", sa.Column("count_blank", sa.Integer(), nullable=True))
    op.add_column("requests", sa.Column("duration_ms", sa.Integer(), nullable=True))


def downgrade_pyp() -> None:
    op.drop_column("requests", "duration_ms")
    op.drop_column("requests", "count_blank")
    op.drop_column("requests", "count_fail")
    op.drop_column("requests", "count_ok")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
