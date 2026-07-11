"""hot path indexes for queue/outbox/schedule/audit scans

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-07-11

P0-15 / roadmap 6.3: partial + composite indexes matching the actual hot
queries (dispatch claim scan, lease reaper, agent requeue, outbox claim,
schedule due scan, batch finalize, monitor/audit listings). Following repo
convention these live in migrations only (ORM declares no Index args).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


def upgrade_pyp() -> None:
    # dispatch claim scan: state=QUEUED(0) ordered by (depth, created_at, id);
    # priority rank comes from tasks so it cannot live in this index
    op.create_index(
        "ix_requests_queued_scan",
        "requests",
        ["depth", "created_at", "id"],
        postgresql_where=sa.text("state = 0"),
    )
    # lease reaper: inflight (ASSIGNED=1/RUNNING=2) with expired lease
    op.create_index(
        "ix_requests_inflight_lease",
        "requests",
        ["lease_until"],
        postgresql_where=sa.text("state IN (1, 2)"),
    )
    # agent disconnect fast-path requeue
    op.create_index(
        "ix_requests_inflight_agent",
        "requests",
        ["agent_id"],
        postgresql_where=sa.text("state IN (1, 2)"),
    )
    # batch finalize / progress / cancel sweep count by (batch, state)
    op.create_index(op.f("ix_requests_batch_id_state"), "requests", ["batch_id", "state"])
    # rule content addressing (agent pull endpoint + get_by_hash)
    op.create_index(op.f("ix_rules_content_hash"), "rules", ["content_hash"])
    # batch listings per task + active batch scan
    op.create_index(op.f("ix_batches_task_id_created_at"), "batches", ["task_id", "created_at"])
    op.create_index(
        "ix_batches_active",
        "batches",
        ["status"],
        postgresql_where=sa.text("status IN ('running', 'canceling')"),
    )
    # schedule due scan (enabled only)
    op.create_index(
        "ix_schedules_due",
        "schedules",
        ["next_run_at"],
        postgresql_where=sa.text("enabled"),
    )
    # outbox claim scan + inflight lease reaper
    op.create_index(
        "ix_push_outbox_pending",
        "push_outbox",
        ["next_retry_at", "id"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_push_outbox_inflight",
        "push_outbox",
        ["lease_until"],
        postgresql_where=sa.text("state = 'inflight'"),
    )
    # node monitor scan
    op.create_index(op.f("ix_agents_status_last_heartbeat"), "agents", ["status", "last_heartbeat"])
    # batch event timeline
    op.create_index(op.f("ix_task_events_batch_id_created_at"), "task_events", ["batch_id", "created_at"])
    # audit listings: recent, by actor, by object
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"])
    op.create_index(op.f("ix_audit_log_actor_id_created_at"), "audit_log", ["actor_id", "created_at"])
    op.create_index(
        op.f("ix_audit_log_object_type_object_id_created_at"),
        "audit_log",
        ["object_type", "object_id", "created_at"],
    )


def downgrade_pyp() -> None:
    op.drop_index(op.f("ix_audit_log_object_type_object_id_created_at"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_actor_id_created_at"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_created_at"), table_name="audit_log")
    op.drop_index(op.f("ix_task_events_batch_id_created_at"), table_name="task_events")
    op.drop_index(op.f("ix_agents_status_last_heartbeat"), table_name="agents")
    op.drop_index("ix_push_outbox_inflight", table_name="push_outbox")
    op.drop_index("ix_push_outbox_pending", table_name="push_outbox")
    op.drop_index("ix_schedules_due", table_name="schedules")
    op.drop_index("ix_batches_active", table_name="batches")
    op.drop_index(op.f("ix_batches_task_id_created_at"), table_name="batches")
    op.drop_index(op.f("ix_rules_content_hash"), table_name="rules")
    op.drop_index(op.f("ix_requests_batch_id_state"), table_name="requests")
    op.drop_index("ix_requests_inflight_agent", table_name="requests")
    op.drop_index("ix_requests_inflight_lease", table_name="requests")
    op.drop_index("ix_requests_queued_scan", table_name="requests")


def upgrade_data_center() -> None:
    # artifact retention/GC scan + provenance lookup (roadmap 6.3)
    op.create_index(op.f("ix_artifacts_expires_at"), "artifacts", ["expires_at"])
    op.create_index(op.f("ix_artifacts_source_id_created_at"), "artifacts", ["source_id", "created_at"])


def downgrade_data_center() -> None:
    op.drop_index(op.f("ix_artifacts_source_id_created_at"), table_name="artifacts")
    op.drop_index(op.f("ix_artifacts_expires_at"), table_name="artifacts")


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
