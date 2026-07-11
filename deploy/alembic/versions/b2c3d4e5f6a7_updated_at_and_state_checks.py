"""updated_at on mutable config tables + state/range checks

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-11

DB-014: mutable config tables gain updated_at (server_default now(); PG11+ fast
default, no table rewrite; historic rows show migration time once). schedules
had no timestamps at all and gains both. Append-only tables (audit_log,
task_events) and hot state machines (requests/batches/push_outbox) keep
created_at only.

DB-012: check constraints ONLY for stable closed sets and numeric ranges.
Additive-evolution string sets (trigger_type, connector_type, notify type,
reason codes) deliberately get NO checks - adding a value there must not
require a migration. requests checks are added NOT VALID then validated to
avoid a long exclusive-lock scan on the largest table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATED_AT_TABLES = (
    "users",
    "roles",
    "permissions",
    "sources",
    "rules",
    "tasks",
    "agents",
    "credentials",
    "api_keys",
    "notify_bots",
    "push_components",
    "assemblies",
    "assembly_watermarks",
    "llm_models",
    "system_prompts",
    "global_params",
    "storage_config",
)

_CHECKS = (
    ("users", "status_valid", "status IN ('active', 'disabled')"),
    ("rules", "status_valid", "status IN ('draft', 'testing', 'active')"),
    ("rules", "version_min", "version >= 1"),
    ("sources", "channel_valid", "channel_default IN ('test', 'prod')"),
    ("sources", "failures_nonneg", "consecutive_failures >= 0"),
    ("sources", "retry_nonneg", "retry >= 0"),
    ("sources", "timeout_positive", "timeout > 0"),
    ("sources", "rate_limit_positive", "rate_limit > 0"),
    # canceling is written by cancel_batch and must be allowed
    ("batches", "status_valid", "status IN ('running', 'canceling', 'done', 'failed', 'canceled')"),
    ("batches", "channel_valid", "channel IN ('test', 'prod')"),
    ("agents", "status_valid", "status IN ('online', 'offline')"),
    ("agents", "slot_n_nonneg", "slot_n >= 0"),
    ("agents", "weight_nonneg", "weight >= 0"),
    ("api_keys", "quota_nonneg", "quota IS NULL OR quota >= 0"),
    ("push_components", "status_valid", "status IN ('draft', 'testing', 'active')"),
    ("push_components", "version_min", "version >= 1"),
    ("push_outbox", "state_valid", "state IN ('pending', 'inflight', 'sent', 'dead')"),
    ("push_outbox", "attempts_nonneg", "attempts >= 0"),
    ("assemblies", "status_valid", "status IN ('draft', 'testing', 'active')"),
    ("assemblies", "script_ver_min", "script_ver >= 1"),
    ("assembly_watermarks", "position_nonneg", "position >= 0"),
    ("system_prompts", "version_min", "version >= 1"),
)

# requests can be large: add NOT VALID, then VALIDATE (SHARE UPDATE EXCLUSIVE only)
_REQUESTS_CHECKS = (
    ("depth_nonneg", "depth >= 0"),
    ("attempt_nonneg", "attempt >= 0"),
    ("state_max", "state <= 4"),  # normal states 0-4; negative error codes open-ended
)


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def _updated_at() -> sa.Column:
    return sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade_pyp() -> None:
    for table in _UPDATED_AT_TABLES:
        op.add_column(table, _updated_at())
    op.add_column(
        "schedules",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("schedules", _updated_at())
    for table, name, expr in _CHECKS:
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, expr)
    for name, expr in _REQUESTS_CHECKS:
        op.create_check_constraint(op.f(f"ck_requests_{name}"), "requests", expr, postgresql_not_valid=True)
        op.execute(f"ALTER TABLE requests VALIDATE CONSTRAINT ck_requests_{name}")


def downgrade_pyp() -> None:
    for name, _expr in _REQUESTS_CHECKS:
        op.drop_constraint(op.f(f"ck_requests_{name}"), "requests", type_="check")
    for table, name, _expr in reversed(_CHECKS):
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.drop_column("schedules", "updated_at")
    op.drop_column("schedules", "created_at")
    for table in reversed(_UPDATED_AT_TABLES):
        op.drop_column(table, "updated_at")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
