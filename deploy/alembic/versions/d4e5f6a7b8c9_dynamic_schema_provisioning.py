"""dynamic schema ledger and source provisioning state

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    op.add_column(
        "sources",
        sa.Column("provisioning_state", sa.String(length=16), server_default="ready", nullable=False),
    )
    op.add_column("sources", sa.Column("provisioning_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sources_provisioning_valid"),
        "sources",
        "provisioning_state IN ('ready', 'provisioning', 'error')",
    )
    op.create_table(
        "dynamic_schemas",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("object_code", sa.String(length=32), nullable=False),
        sa.Column("database_name", sa.String(length=32), nullable=False),
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("indexed_fields", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="provisioning", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('data', 'assembly')", name=op.f("ck_dynamic_schemas_kind_valid")),
        sa.CheckConstraint("schema_version >= 1", name=op.f("ck_dynamic_schemas_schema_version_min")),
        sa.CheckConstraint(
            "status IN ('ready', 'provisioning', 'error')", name=op.f("ck_dynamic_schemas_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dynamic_schemas")),
        sa.UniqueConstraint("kind", "object_code", name="uq_dynamic_schemas_kind_code"),
    )


def downgrade_pyp() -> None:
    op.drop_table("dynamic_schemas")
    op.drop_constraint(op.f("ck_sources_provisioning_valid"), "sources", type_="check")
    op.drop_column("sources", "provisioning_error")
    op.drop_column("sources", "provisioning_state")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
