"""M3 slice-8 集成测试（需 PG）：增量组装 —— 读侧水位只读增量 + 写侧指纹幂等 + 读腿可重算（reset）。"""

from __future__ import annotations

import asyncio

from payipa.crawl.ingest import build_data_table, create_data_table, drop_data_table
from payipa.db.settings import get_settings
from payipa.studio.asm import build_asm_table, drop_asm_table
from payipa.studio.run import run_assembly
from payipa.studio.store import AssemblyStore
from payipa.studio.watermark import get_watermarks, reset_watermarks
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m3wm"
_PROD = "m3wmprod"


async def _seed(dc, table, start: int, titles: list[str]) -> None:
    async with dc.begin() as conn:
        for i, t in enumerate(titles, start=start):
            await conn.execute(pg_insert(table).values(data_fingerprint=f"fp{i}", state=3, fields={"title": t}))


def test_incremental_watermark(require_pg: None) -> None:
    async def main() -> None:
        dc = create_async_engine(get_settings().async_url("data_center"))
        biz = create_async_engine(get_settings().async_url("business"))
        pyp = create_async_engine(get_settings().async_url("pyp"))
        src = build_data_table(_UUID, [])
        asm = build_asm_table(_PROD, [])

        async def script(ctx) -> list[dict]:
            data = await ctx.read_table(_UUID, columns=["title"], incremental=True)
            return [{"title": r["title"]} for r in data]

        async def asm_count() -> int:
            async with biz.connect() as conn:
                return int((await conn.execute(select(func.count()).select_from(asm))).scalar())

        try:
            await drop_data_table(dc, src)
            await drop_asm_table(biz, asm)
            await create_data_table(dc, src)
            aid, _, _ = await AssemblyStore(pyp).put(
                name="wm-asm", product_code=_PROD, script_ref="inline", fingerprint_keys=["title"]
            )

            # 首轮：3 行全新 → 读 3、水位推进到该源最大 id
            await _seed(dc, src, 0, ["a", "b", "c"])
            n1 = await run_assembly(
                dc,
                biz,
                product_code=_PROD,
                script=script,
                assembly_id=aid,
                fingerprint_keys=["title"],
                engine_pyp=pyp,
                incremental=True,
            )
            assert n1 == 3 and await asm_count() == 3
            wm1 = await get_watermarks(pyp, aid)
            assert wm1.get(_UUID, 0) > 0
            first_pos = wm1[_UUID]

            # 二轮：再加 2 行 → 增量只读新增 2（不重读旧 3）→ asm_ 累积到 5
            await _seed(dc, src, 100, ["d", "e"])
            n2 = await run_assembly(
                dc,
                biz,
                product_code=_PROD,
                script=script,
                assembly_id=aid,
                fingerprint_keys=["title"],
                engine_pyp=pyp,
                incremental=True,
            )
            assert n2 == 2, f"增量应只读新增 2 行，实际读 {n2}"
            assert await asm_count() == 5
            assert (await get_watermarks(pyp, aid))[_UUID] > first_pos

            # 三轮：无新增 → 增量读 0
            n3 = await run_assembly(
                dc,
                biz,
                product_code=_PROD,
                script=script,
                assembly_id=aid,
                fingerprint_keys=["title"],
                engine_pyp=pyp,
                incremental=True,
            )
            assert n3 == 0 and await asm_count() == 5

            # 读腿可重算：清水位 → 重读全部 5，但写腿指纹幂等 → asm_ 仍 5 行
            assert await reset_watermarks(pyp, aid) == 1
            assert await get_watermarks(pyp, aid) == {}
            n4 = await run_assembly(
                dc,
                biz,
                product_code=_PROD,
                script=script,
                assembly_id=aid,
                fingerprint_keys=["title"],
                engine_pyp=pyp,
                incremental=True,
            )
            assert n4 == 5, f"reset 后应重读全部 5，实际 {n4}"
            assert await asm_count() == 5  # 幂等：无重复
        finally:
            async with pyp.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM assembly_watermarks WHERE assembly_id IN (SELECT id FROM assemblies WHERE name='wm-asm')"
                    )
                )
                await conn.execute(text("DELETE FROM assemblies WHERE name='wm-asm'"))
            await drop_data_table(dc, src)
            await drop_asm_table(biz, asm)
            await dc.dispose()
            await biz.dispose()
            await pyp.dispose()

    asyncio.run(main())
