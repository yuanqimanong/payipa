"""增量组装读侧水位（M3 slice-8，02/04 定案「增量范式 = 读侧水位 + 写侧幂等」）。

某组装从某数据源已消费到的最大 data_* id 存 `assembly_watermarks`。下次组装只读 id > 水位的增量行；
写腿 asm_ 指纹唯一索引幂等去重，故即使读区间重叠也不产重复。**读腿可重算**：`reset_watermarks`
清零即从头重读（幂等写保证安全）。水位仅前进（advance 取 max），并发/乱序回报不回退。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import AssemblyWatermark


async def get_watermarks(engine_pyp: AsyncEngine, assembly_id: int) -> dict[str, int]:
    """取某组装各数据源的当前水位 {source: position}（无记录的源不在字典中，视作 0）。"""
    async with engine_pyp.begin() as conn:
        rows = (
            await conn.execute(
                select(AssemblyWatermark.source, AssemblyWatermark.position).where(
                    AssemblyWatermark.assembly_id == assembly_id
                )
            )
        ).all()
    return {r.source: int(r.position) for r in rows}


async def advance_watermarks(engine_pyp: AsyncEngine, assembly_id: int, positions: dict[str, int]) -> None:
    """把各源水位推进到 positions（仅前进：ON CONFLICT 时取 GREATEST，不回退）。空值/0 也可安全 upsert。"""
    if not positions:
        return
    async with engine_pyp.begin() as conn:
        for source, position in positions.items():
            stmt = pg_insert(AssemblyWatermark.__table__).values(
                assembly_id=assembly_id, source=source, position=int(position)
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_assembly_watermark",
                set_={"position": func.greatest(stmt.excluded.position, AssemblyWatermark.position)},
            )
            await conn.execute(stmt)


async def reset_watermarks(engine_pyp: AsyncEngine, assembly_id: int, source: str | None = None) -> int:
    """清零水位（读腿可重算）：source=None 清该组装所有源，否则只清该源。返回删除行数。"""
    stmt = delete(AssemblyWatermark.__table__).where(AssemblyWatermark.assembly_id == assembly_id)
    if source is not None:
        stmt = stmt.where(AssemblyWatermark.source == source)
    async with engine_pyp.begin() as conn:
        res = await conn.execute(stmt)
    return res.rowcount or 0


__all__ = ["advance_watermarks", "get_watermarks", "reset_watermarks"]
