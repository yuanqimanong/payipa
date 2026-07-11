"""M3 slice-9 集成测试（需 PG）：SchemaEvolver —— asm_ 加法演进（新增索引列）+ 破坏性删除拦截 + 注入防护。"""

from __future__ import annotations

import asyncio

import pytest
from payipa.db.settings import get_settings
from payipa.studio.asm import AsmLoader, build_asm_table, drop_asm_table
from payipa.studio.evolve import BreakingSchemaChange, evolve_asm_table
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

_PROD = "m3evo"


async def _idx_columns(biz, name: str) -> set[str]:
    async with biz.connect() as conn:
        return set(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name=:n AND column_name LIKE 'idx\\_%'"
                    ),
                    {"n": name},
                )
            )
            .scalars()
            .all()
        )


def test_schema_evolver(require_pg: None) -> None:
    async def main() -> None:
        biz = create_async_engine(get_settings().async_url("business"))
        name = f"asm_{_PROD}"
        t_a = build_asm_table(_PROD, ["a"])
        t_ab = build_asm_table(_PROD, ["a", "b"])
        t_a2 = build_asm_table(_PROD, ["a"])
        try:
            await drop_asm_table(biz, t_a)

            # 1) 表不存在 → 建表（含 idx_a）
            r1 = await evolve_asm_table(biz, t_a)
            assert r1["created"] is True
            assert await _idx_columns(biz, name) == {"idx_a"}

            # 播一行（fields 带 a,b）——此时无 idx_b 列
            await AsmLoader(biz).upsert(t_a, [{"a": "x1", "b": "y1"}], fingerprint_keys=["a"])

            # 2) 加法演进：目标含 idx_a+idx_b → 只新增 idx_b（STORED 生成列，旧行自动回填）
            r2 = await evolve_asm_table(biz, t_ab)
            assert r2 == {"created": False, "added": ["idx_b"], "removed": []}
            assert await _idx_columns(biz, name) == {"idx_a", "idx_b"}
            async with biz.connect() as conn:
                # 生成列可用于过滤，旧行的 idx_b 已由 fields->>'b' 回填
                got = (await conn.execute(select(t_ab.c["idx_b"]).where(t_ab.c["idx_a"] == "x1"))).scalar()
            assert got == "y1"

            # 3) 破坏性（删除 idx_b）默认拦截
            with pytest.raises(BreakingSchemaChange):
                await evolve_asm_table(biz, t_a2)
            assert await _idx_columns(biz, name) == {"idx_a", "idx_b"}  # 未改

            # 4) allow_breaking=True → 真删 idx_b
            r4 = await evolve_asm_table(biz, t_a2, allow_breaking=True)
            assert r4["removed"] == ["idx_b"]
            assert await _idx_columns(biz, name) == {"idx_a"}

            # 5) 幂等：无变化再演进 → 空
            assert await evolve_asm_table(biz, t_a2) == {"created": False, "added": [], "removed": []}

            # 6) 不安全字段名 → ValueError（DDL 注入防护；统一校验已前移到 build_asm_table，P0-13）
            with pytest.raises(ValueError, match="非法"):
                await evolve_asm_table(biz, build_asm_table(_PROD, ["a; DROP TABLE x"]))
        finally:
            await drop_asm_table(biz, t_ab)
            await biz.dispose()

    asyncio.run(main())
