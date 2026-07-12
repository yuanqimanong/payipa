"""queue legacy sources without a production schema ledger for reconciliation

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    op.execute(
        """
        UPDATE sources AS source
        SET provisioning_state = 'provisioning',
            provisioning_error = NULL
        WHERE NOT EXISTS (
            SELECT 1
            FROM dynamic_schemas AS schema
            WHERE schema.kind = 'data'
              AND schema.object_code = source.uuid
              AND schema.channel = 'prod'
              AND schema.status = 'ready'
        )
        """
    )


def downgrade_pyp() -> None:
    # Reconciliation is a forward data repair. A downgrade must not falsify or discard its result.
    pass


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
