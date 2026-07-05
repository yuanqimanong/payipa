"""规则内容寻址存储（pyp.rules）。content_hash 为不可变缓存键；agent 按 hash 拉取。"""

from __future__ import annotations

import json

from jianbing_utils import crypto
from payipa_contracts import RulePack, RulePointer
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
        """登记规则（按 content_hash 去重）；返回可放入 TaskSpec 的指针。"""
        digest = content_hash(pack)
        async with self.engine.begin() as conn:
            existing = (await conn.execute(select(Rule.id, Rule.version).where(Rule.content_hash == digest))).first()
            if existing:
                return RulePointer(rule_id=str(existing[0]), version=existing[1], content_hash=digest)
            max_v = (await conn.execute(select(func.max(Rule.version)).where(Rule.source_id == source_id))).scalar()
            version = (max_v or 0) + 1
            rule_id = (
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
                    .returning(Rule.id)
                )
            ).scalar_one()
        return RulePointer(rule_id=str(rule_id), version=version, content_hash=digest)

    async def get_by_hash(self, digest: str) -> RulePack | None:
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(Rule.spec).where(Rule.content_hash == digest))).first()
        return RulePack.model_validate(row[0]) if row else None
