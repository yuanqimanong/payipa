"""one-time agent enrollment and outbox claim fencing

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    op.create_table(
        "agent_enrollments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_agent_enrollments_created_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_enrollments")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_agent_enrollments_token_hash")),
    )
    op.create_index(
        "ix_agent_enrollments_expires_unused",
        "agent_enrollments",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.add_column("push_outbox", sa.Column("claim_token", sa.String(length=64), nullable=True))


def downgrade_pyp() -> None:
    op.drop_column("push_outbox", "claim_token")
    op.drop_index("ix_agent_enrollments_expires_unused", table_name="agent_enrollments")
    op.drop_table("agent_enrollments")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
