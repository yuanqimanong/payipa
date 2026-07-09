"""push_outbox: partial-unique index on idempotency_key (M4 slice-1)

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()


def upgrade_pyp() -> None:
    # 幂等去重：同 idempotency_key 只入 outbox 一次。全列唯一索引——PG 默认 NULL 互异，故允许多条无 key 行；
    # ON CONFLICT (idempotency_key) 可直接推断该索引（无需谓词）。
    op.create_index("uq_push_outbox_idempotency_key", "push_outbox", ["idempotency_key"], unique=True)


def downgrade_pyp() -> None:
    op.drop_index("uq_push_outbox_idempotency_key", table_name="push_outbox")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
