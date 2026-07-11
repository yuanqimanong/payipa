"""SchemaEvolver（M3 slice-9）：asm_/data_ 动态表**加法演进** + 破坏性变更拦截。

混合 schema 下用户字段进 JSONB `fields`（加键免 DDL、天然加法）；唯一需 DDL 演进的是「勾索引」字段——
每个索引字段一列 `idx_<字段>`（STORED 生成列 + B-tree）。本模块对比目标表与库中现状：
- **加法**（新增索引字段）：`ALTER TABLE ADD COLUMN idx_<f> ... GENERATED ALWAYS AS ((fields->>'f')) STORED` + 建索引；
- **破坏性**（删除已有索引字段）：默认**拦截**（抛 BreakingSchemaChange），除非显式 allow_breaking=True。

表名/字段名统一走 payipa.db.ident 校验（防 DDL 注入，与 asm.py 的内插假设一致）。跨库不 join；只碰目标表自身。
"""

from __future__ import annotations

from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.ident import check_field, check_ident
from payipa.studio.asm import create_asm_table


class BreakingSchemaChange(RuntimeError):
    """检测到破坏性表结构变更（删除索引列等），默认拦截不执行。"""


async def _table_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        return bool((await conn.execute(text("SELECT to_regclass(:n)"), {"n": name})).scalar())


async def _existing_idx_columns(engine: AsyncEngine, name: str) -> set[str]:
    """库中该表以 idx_ 开头的列（= 现有索引字段生成列）。"""
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :n AND column_name LIKE 'idx\\_%'"
                    ),
                    {"n": name},
                )
            )
            .scalars()
            .all()
        )
    return set(rows)


async def evolve_asm_table(engine_business: AsyncEngine, table: Table, *, allow_breaking: bool = False) -> dict:
    """把库中 asm_ 表演进到目标 table 的索引列集合。表不存在则建。返回 {created, added, removed}。

    加法（新增 idx_<f>）总是执行；破坏性（删除现有 idx_<f>）默认抛 BreakingSchemaChange，allow_breaking=True 时才 DROP。
    """
    name = check_ident(table.name)  # 表名同样会内插进裸 DDL（ALTER/CREATE INDEX），先过统一校验
    desired = {c.name for c in table.columns if c.name.startswith("idx_")}
    for col in desired:
        check_field(col[len("idx_") :])  # 先校验目标字段名（DDL 注入防护），越界即抛 ValueError，不落任何 DDL

    if not await _table_exists(engine_business, name):
        await create_asm_table(engine_business, table)
        return {"created": True, "added": sorted(desired), "removed": []}

    have = await _existing_idx_columns(engine_business, name)
    to_add = sorted(desired - have)
    to_remove = sorted(have - desired)

    if to_remove and not allow_breaking:
        raise BreakingSchemaChange(
            f"{name}: dropping indexed column(s) {to_remove} is a breaking change; pass allow_breaking=True to force"
        )

    async with engine_business.begin() as conn:
        for col in to_add:
            field = check_field(col[len("idx_") :])
            await conn.execute(
                text(
                    f'ALTER TABLE "{name}" ADD COLUMN "{col}" text '
                    f"GENERATED ALWAYS AS ((fields ->> '{field}')) STORED"
                )
            )
            await conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_{name}_{col}" ON "{name}" ("{col}")'))
        if allow_breaking:
            for col in to_remove:
                check_ident(col)  # 库中存量列名只需字符集安全即可 DROP（长度可能超新字段上限）
                await conn.execute(text(f'ALTER TABLE "{name}" DROP COLUMN IF EXISTS "{col}"'))

    return {"created": False, "added": to_add, "removed": to_remove if allow_breaking else []}


__all__ = ["BreakingSchemaChange", "evolve_asm_table"]
