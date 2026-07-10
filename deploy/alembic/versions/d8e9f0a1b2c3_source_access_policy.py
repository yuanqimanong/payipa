"""source access policy and pause state

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    # 存量部署清理：应用内动态网络路由已撤下（决策记录 2026-07-10），供应商凭证表不得残留；
    # 全新安装的初始迁移已不再创建这些对象，故用 IF EXISTS。
    op.execute("ALTER TABLE sources DROP COLUMN IF EXISTS proxy_config_id")
    op.execute("DROP TABLE IF EXISTS proxy_usage")
    op.execute("DROP TABLE IF EXISTS proxy_providers")
    op.execute("DROP TABLE IF EXISTS proxy_configs")
    op.add_column("sources", sa.Column("access_basis", sa.String(length=32), nullable=True))
    op.add_column("sources", sa.Column("access_reference", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("access_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("pause_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sources_access_basis"),
        "sources",
        "access_basis IS NULL OR access_basis IN ('owned', 'contracted', 'public_policy')",
    )


def downgrade_pyp() -> None:
    # 撤下的网络路由对象不随 downgrade 复活（能力已从产品中移除）。
    op.drop_constraint(op.f("ck_sources_access_basis"), "sources", type_="check")
    op.drop_column("sources", "pause_reason")
    op.drop_column("sources", "paused_at")
    op.drop_column("sources", "access_confirmed_at")
    op.drop_column("sources", "access_reference")
    op.drop_column("sources", "access_basis")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
