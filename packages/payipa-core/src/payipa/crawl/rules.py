"""规则内容寻址存储（pyp.rules）。content_hash 为不可变缓存键；agent 按 hash 拉取。"""

from __future__ import annotations

import json

from jianbing_utils import crypto
from payipa_contracts import RulePack, RulePointer, RuleStatus
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import Rule


def content_hash(pack: RulePack) -> str:
    """规则内容的规范化 sha256（字段排序，确定性）。"""
    blob = json.dumps(pack.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return crypto.sha256(blob)


class RuleStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def put(
        self, source_id: int, pack: RulePack, *, status: str = "active", created_by: int | None = None
    ) -> RulePointer:
        """登记规则（按 (source_id, content_hash) 源内去重）；返回可放入 TaskSpec 的指针。

        跨源相同 spec 各自成行（DB-001）：归属、版本、派发互不串。并发重复 put 依赖
        唯一约束 uq_rules_source_id_content_hash 兜底，冲突后重查返回已有行。
        """
        status = RuleStatus(status).value
        digest = content_hash(pack)
        async with self.engine.begin() as conn:
            existing = await self._find(conn, source_id, digest)
            if existing:
                return existing
            max_v = (await conn.execute(select(func.max(Rule.version)).where(Rule.source_id == source_id))).scalar()
            version = (max_v or 0) + 1
            row = (
                await conn.execute(
                    pg_insert(Rule.__table__)
                    .values(
                        source_id=source_id,
                        version=version,
                        content_hash=digest,
                        status=status,
                        spec=pack.model_dump(mode="json"),
                        created_by=created_by,
                    )
                    .on_conflict_do_nothing(constraint="uq_rules_source_id_content_hash")
                    .returning(Rule.id)
                )
            ).first()
        if row is not None:
            return RulePointer(rule_id=str(row[0]), version=version, content_hash=digest)
        # 并发对手先插成功：重查取已有行
        async with self.engine.begin() as conn:
            existing = await self._find(conn, source_id, digest)
        if existing is None:  # pragma: no cover - 仅约束异常时可达
            raise RuntimeError(f"规则登记冲突后重查失败：source_id={source_id} hash={digest[:12]}")
        return existing

    @staticmethod
    async def _find(conn, source_id: int, digest: str) -> RulePointer | None:
        row = (
            await conn.execute(
                select(Rule.id, Rule.version).where(Rule.source_id == source_id, Rule.content_hash == digest)
            )
        ).first()
        return RulePointer(rule_id=str(row[0]), version=row[1], content_hash=digest) if row else None

    async def get_by_hash(self, digest: str) -> RulePack | None:
        """按内容哈希取 spec。hash 只在源内唯一，但内容寻址保证同 hash 同 spec，任取一行即可。"""
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(Rule.spec).where(Rule.content_hash == digest).limit(1))).first()
        return RulePack.model_validate(row[0]) if row else None
