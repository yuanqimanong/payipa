"""collection resilience state

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    op.add_column("sources", sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("cooldown_reason", sa.String(length=64), nullable=True))
    op.add_column("sources", sa.Column("last_status_code", sa.SmallInteger(), nullable=True))
    op.add_column(
        "sources", sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("sources", sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_sources_cooldown_until"), "sources", ["cooldown_until"], unique=False)

    op.add_column("requests", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    op.add_column("requests", sa.Column("response_status", sa.SmallInteger(), nullable=True))
    op.add_column("requests", sa.Column("reason_code", sa.String(length=64), nullable=True))
    op.add_column("requests", sa.Column("error_detail", sa.Text(), nullable=True))
    op.add_column("requests", sa.Column("retry_after_s", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_requests_not_before"), "requests", ["not_before"], unique=False)


def downgrade_pyp() -> None:
    op.drop_index(op.f("ix_requests_not_before"), table_name="requests")
    op.drop_column("requests", "retry_after_s")
    op.drop_column("requests", "error_detail")
    op.drop_column("requests", "reason_code")
    op.drop_column("requests", "response_status")
    op.drop_column("requests", "not_before")

    op.drop_index(op.f("ix_sources_cooldown_until"), table_name="sources")
    op.drop_column("sources", "last_failure_at")
    op.drop_column("sources", "last_success_at")
    op.drop_column("sources", "consecutive_failures")
    op.drop_column("sources", "last_status_code")
    op.drop_column("sources", "cooldown_reason")
    op.drop_column("sources", "cooldown_until")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
