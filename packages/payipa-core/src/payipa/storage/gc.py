"""对象/raw 保留期回收（GC）。删已过 expires_at 的 local 工件：先删对象、后删登记行。

raw 默认保留 7 天（按源可配，02 定案）；与 artifact GC 合并。调度触发留 M2（cron）。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.data_center import Artifact
from payipa.storage.base import StorageBackend


async def gc_expired_artifacts(engine_dc: AsyncEngine, storage: StorageBackend, *, limit: int = 1000) -> int:
    """清理已过保留期（expires_at < now）的 local 工件；返回清理数量。"""
    async with engine_dc.begin() as conn:
        rows = (
            await conn.execute(
                select(Artifact.id, Artifact.object_key)
                .where(
                    Artifact.expires_at.is_not(None),
                    Artifact.expires_at < func.now(),
                    Artifact.storage_backend == "local",
                )
                .limit(limit)
            )
        ).all()
        removed = 0
        for art_id, object_key in rows:
            await storage.delete(object_key)  # 先删对象
            await conn.execute(delete(Artifact.__table__).where(Artifact.id == art_id))  # 后删登记
            removed += 1
    return removed
