"""isolate dynamic schema ledger entries by test/prod channel

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    op.add_column(
        "dynamic_schemas",
        sa.Column("channel", sa.String(length=8), server_default="prod", nullable=False),
    )
    op.drop_constraint("uq_dynamic_schemas_kind_code", "dynamic_schemas", type_="unique")
    op.create_check_constraint(
        op.f("ck_dynamic_schemas_channel_valid"),
        "dynamic_schemas",
        "channel IN ('test', 'prod')",
    )
    op.create_unique_constraint(
        "uq_dynamic_schemas_kind_code_channel",
        "dynamic_schemas",
        ["kind", "object_code", "channel"],
    )


def downgrade_pyp() -> None:
    op.drop_constraint("uq_dynamic_schemas_kind_code_channel", "dynamic_schemas", type_="unique")
    op.drop_constraint(op.f("ck_dynamic_schemas_channel_valid"), "dynamic_schemas", type_="check")
    op.execute("DELETE FROM dynamic_schemas WHERE channel = 'test'")
    op.drop_column("dynamic_schemas", "channel")
    op.create_unique_constraint(
        "uq_dynamic_schemas_kind_code",
        "dynamic_schemas",
        ["kind", "object_code"],
    )


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
