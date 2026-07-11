"""rules scoped dedup + requests rule fk

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-07-11

DB-001/DB-002 (P0-12): rule dedup becomes per-source (source_id, content_hash);
requests.rule_id becomes the authoritative FK to rules.id (backfilled from the
legacy rule_hash join, with cross-source ownership repaired by copying the rule
into the request's own source). rule_hash/rule_version stay as immutable
dispatch-time snapshots for lineage only. Downgrade drops constraints but keeps
backfilled data (copied rule rows are not un-split; lossy by design).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(engine_name: str) -> None:
    globals()[f"upgrade_{engine_name}"]()


def downgrade(engine_name: str) -> None:
    globals()[f"downgrade_{engine_name}"]()


_POINT_TO_OWN_RULE = """
UPDATE requests SET rule_id = own.rid
FROM (
    SELECT r.id AS req_id, MIN(ru.id) AS rid
    FROM requests r
    JOIN batches b ON r.batch_id = b.id
    JOIN tasks t ON b.task_id = t.id
    JOIN rules ru ON ru.source_id = t.source_id AND ru.content_hash = r.rule_hash
    WHERE r.rule_hash IS NOT NULL
    GROUP BY r.id
) AS own
WHERE requests.id = own.req_id
"""

_COPY_MISSING_RULES = """
INSERT INTO rules (source_id, version, content_hash, status, spec, created_by, created_at)
SELECT n.source_id,
       COALESCE(mv.maxv, 0) + ROW_NUMBER() OVER (PARTITION BY n.source_id ORDER BY n.content_hash),
       n.content_hash, d.status, d.spec, d.created_by, now()
FROM (
    SELECT DISTINCT t.source_id, r.rule_hash AS content_hash
    FROM requests r
    JOIN batches b ON r.batch_id = b.id
    JOIN tasks t ON b.task_id = t.id
    WHERE r.rule_hash IS NOT NULL AND r.rule_id IS NULL
) AS n
JOIN (
    SELECT DISTINCT ON (content_hash) content_hash, status, spec, created_by
    FROM rules ORDER BY content_hash, id
) AS d ON d.content_hash = n.content_hash
LEFT JOIN (
    SELECT source_id, MAX(version) AS maxv FROM rules GROUP BY source_id
) AS mv ON mv.source_id = n.source_id
"""

_POINT_DUPES_TO_KEEPER = """
UPDATE requests SET rule_id = k.keeper
FROM rules ru
JOIN (
    SELECT source_id, content_hash, MIN(id) AS keeper
    FROM rules GROUP BY source_id, content_hash
) AS k ON k.source_id = ru.source_id AND k.content_hash = ru.content_hash
WHERE requests.rule_id = ru.id AND ru.id <> k.keeper
"""

_DELETE_DUPES = """
DELETE FROM rules WHERE id IN (
    SELECT ru.id
    FROM rules ru
    JOIN (
        SELECT source_id, content_hash, MIN(id) AS keeper
        FROM rules GROUP BY source_id, content_hash
    ) AS k ON k.source_id = ru.source_id AND k.content_hash = ru.content_hash
    WHERE ru.id <> k.keeper
)
"""


def upgrade_pyp() -> None:
    # 1) point requests at their own source's rule row (legacy global dedup
    #    means rule_hash is still an unambiguous lookup key at this moment)
    op.execute(_POINT_TO_OWN_RULE)
    # 2) requests whose own source never got a rule row (cross-source reuse
    #    victims): copy the rule into that source with the next free version
    op.execute(_COPY_MISSING_RULES)
    op.execute(_POINT_TO_OWN_RULE)
    # 3) collapse accidental per-source duplicates before the unique lands
    op.execute(_POINT_DUPES_TO_KEEPER)
    op.execute(_DELETE_DUPES)
    # 4) invariants: per-source dedup unique (explicit name: the naming
    #    convention only uses the first column and would collide with
    #    uq_rules_source_id), FK + index for the authoritative reference
    op.create_unique_constraint("uq_rules_source_id_content_hash", "rules", ["source_id", "content_hash"])
    op.create_index(op.f("ix_requests_rule_id"), "requests", ["rule_id"], unique=False)
    op.create_foreign_key(op.f("fk_requests_rule_id_rules"), "requests", "rules", ["rule_id"], ["id"])


def downgrade_pyp() -> None:
    op.drop_constraint(op.f("fk_requests_rule_id_rules"), "requests", type_="foreignkey")
    op.drop_index(op.f("ix_requests_rule_id"), table_name="requests")
    op.drop_constraint("uq_rules_source_id_content_hash", "rules", type_="unique")


def upgrade_data_center() -> None:
    pass


def downgrade_data_center() -> None:
    pass


def upgrade_business() -> None:
    pass


def downgrade_business() -> None:
    pass
