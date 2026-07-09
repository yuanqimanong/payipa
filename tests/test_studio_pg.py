"""M3 slice-1 集成测试（需 PG）：data_* → 进程内 Query Gateway（过滤/keyset）→ LocalExecutor 组装 → asm_* 幂等装载。"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from payipa.crawl.ingest import build_data_table, create_data_table, drop_data_table
from payipa.db.settings import get_settings
from payipa.studio.asm import asm_table_name, build_asm_table, drop_asm_table
from payipa.studio.gateway import QueryGateway
from payipa.studio.run import run_assembly
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m3src"
_PROD = "m3prod"


async def _seed(dc, table, titles: list[str]) -> None:
    async with dc.begin() as conn:
        for i, t in enumerate(titles):
            await conn.execute(
                pg_insert(table).values(data_fingerprint=f"fp{i}", state=3, fields={"title": t, "n": len(t)})
            )


def test_studio_assembly_pipeline(require_pg: None) -> None:
    async def main() -> None:
        dc = create_async_engine(get_settings().async_url("data_center"))
        biz = create_async_engine(get_settings().async_url("business"))
        src = build_data_table(_UUID, [])
        asm = build_asm_table(_PROD, ["upper"])
        try:
            await drop_data_table(dc, src)
            await drop_asm_table(biz, asm)
            await create_data_table(dc, src)
            await _seed(dc, src, ["alpha", "beta", "gamma"])

            # 1) Query Gateway 直读：投影 + keyset 翻页（limit=1 → 3 页，无重无漏）
            gw = QueryGateway()
            seen, cursor, pages = [], None, 0
            while True:
                req = c.TableQueryRequest(source=_UUID, limit=1, cursor=cursor)
                rows, cursor, quota = await gw.read(dc, req)
                pages += 1
                seen += [r["fields"]["title"] for r in rows]
                assert quota.rows_returned == len(rows)
                if cursor is None:
                    break
            assert seen == ["alpha", "beta", "gamma"] and pages == 3

            # 2) 过滤：CONTAINS 'a' → alpha/gamma/beta 均含 a? alpha,gamma,beta 都含 'a' → 用 EQ 精确
            rows, _, _ = await gw.read(
                dc,
                c.TableQueryRequest(
                    source=_UUID, filters=[c.ColumnFilter(column="title", op=c.FilterOp.EQ, value="beta")]
                ),
            )
            assert [r["fields"]["title"] for r in rows] == ["beta"]

            # 3) 组装：读源 → 产出 {upper, length}；LocalExecutor + asm_* 幂等装载
            async def script(ctx) -> list[dict]:
                data = await ctx.read_table(_UUID)
                return [{"upper": r["fields"]["title"].upper(), "length": r["fields"]["n"]} for r in data]

            n1 = await run_assembly(
                dc,
                biz,
                product_code=_PROD,
                script=script,
                assembly_id=7,
                fingerprint_keys=["upper"],
                indexed_fields=["upper"],
            )
            assert n1 == 3
            async with biz.connect() as conn:
                cnt = (await conn.execute(select(func.count()).select_from(asm))).scalar()
                sample = (await conn.execute(select(asm.c["fields"]).order_by(asm.c["id"]))).scalars().all()
            assert cnt == 3
            assert {s["upper"] for s in sample} == {"ALPHA", "BETA", "GAMMA"}

            # 4) 重跑幂等：指纹相同 → 不新增行（upsert）
            n2 = await run_assembly(
                dc, biz, product_code=_PROD, script=script, fingerprint_keys=["upper"], indexed_fields=["upper"]
            )
            assert n2 == 3
            async with biz.connect() as conn:
                cnt2 = (await conn.execute(select(func.count()).select_from(asm))).scalar()
            assert cnt2 == 3  # 幂等：仍 3 行，无重复
            assert asm_table_name(_PROD) == "asm_m3prod"
        finally:
            await drop_data_table(dc, src)
            await drop_asm_table(biz, asm)
            await dc.dispose()
            await biz.dispose()

    asyncio.run(main())
