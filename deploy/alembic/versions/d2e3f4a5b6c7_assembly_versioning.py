"""assemblies: versioning + content-address + sign gate + product config (M3 slice-5)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()


def upgrade_pyp() -> None:
    op.add_column("assemblies", sa.Column("product_code", sa.String(length=32), nullable=True))
    op.add_column("assemblies", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.add_column("assemblies", sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"))
    op.add_column("assemblies", sa.Column("script_ref", sa.Text(), nullable=True))
    op.add_column("assemblies", sa.Column("signature", sa.String(length=128), nullable=True))
    op.add_column(
        "assemblies",
        sa.Column("fingerprint_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
    )
    op.add_column(
        "assemblies",
        sa.Column("indexed_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
    )


def downgrade_pyp() -> None:
    for col in (
        "indexed_fields",
        "fingerprint_keys",
        "signature",
        "script_ref",
        "status",
        "content_hash",
        "product_code",
    ):
        op.drop_column("assemblies", col)


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
