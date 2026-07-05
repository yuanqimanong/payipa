"""M1-5 集成测试（需 PG）：QueryService filter/sort/page 正确。"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from payipa.crawl.ingest import Ingestor, build_data_table, create_data_table, drop_data_table
from payipa.db.settings import get_settings
from payipa.explore.query import query_data
from sqlalchemy.ext.asyncio import create_async_engine


def _item(title: str) -> c.Item:
    return c.Item(fields={"title": title, "price": "1"}, field_meta={})


def test_query_filter_sort_page(require_pg: None) -> None:
    async def main() -> None:
        engine = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table("m1q", indexed_fields=["title"])
        try:
            await drop_data_table(engine, table)
            await create_data_table(engine, table)
            await Ingestor(engine).upsert(
                table,
                [_item("Alpha"), _item("Beta"), _item("Gamma")],
                batch_id=1,
                fingerprint_keys=["title"],
            )

            # 全部 + 字段展开
            allrows = await query_data(engine, table, page=1, size=50)
            assert allrows["total"] == 3
            assert len(allrows["data"]) == 3
            assert {"id", "created_at", "state", "title", "price"} <= set(allrows["data"][0])

            # 过滤（ilike）
            filtered = await query_data(engine, table, filters=[{"field": "title", "type": "like", "value": "Alph"}])
            assert filtered["total"] == 1
            assert filtered["data"][0]["title"] == "Alpha"

            # 分页
            paged = await query_data(engine, table, page=1, size=2)
            assert len(paged["data"]) == 2
            assert paged["last_page"] == 2

            # 排序（title 升序）
            asc = await query_data(engine, table, sorters=[{"field": "title", "dir": "asc"}])
            assert [d["title"] for d in asc["data"]] == ["Alpha", "Beta", "Gamma"]
        finally:
            await drop_data_table(engine, table)
            await engine.dispose()

    asyncio.run(main())
