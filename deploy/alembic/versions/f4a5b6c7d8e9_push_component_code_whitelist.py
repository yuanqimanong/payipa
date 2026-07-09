"""push_components: inline code + target-domain whitelist + publish signature (M4 slice-3)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()


def upgrade_pyp() -> None:
    # 推送组件：内联源码（固定方法 push(ctx)）+ 目标域白名单（隔离子进程出网仅放行这些）+ 发布签名（红线7）。
    op.add_column("push_components", sa.Column("code", sa.Text(), nullable=True))
    op.add_column(
        "push_components",
        sa.Column("allow_domains", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column("push_components", sa.Column("signature", sa.Text(), nullable=True))


def downgrade_pyp() -> None:
    op.drop_column("push_components", "signature")
    op.drop_column("push_components", "allow_domains")
    op.drop_column("push_components", "code")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
