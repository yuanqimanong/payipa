"""M1-1 集成测试（需 PG）：动态 data_ 表建表、指纹 upsert 幂等去重、STORED 生成列可查。"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from payipa.crawl.ingest import (
    Ingestor,
    build_data_table,
    create_data_table,
    drop_data_table,
)
from payipa.db.settings import get_settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine


def _item(title: str, price: str) -> c.Item:
    return c.Item(
        fields={"title": title, "price": price},
        field_meta={"title": c.FieldMeta(raw_value=title, normalized_value=title, confidence=1.0)},
    )


def test_dynamic_table_upsert_dedup(require_pg: None) -> None:
    async def main() -> None:
        engine = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table("m1test", indexed_fields=["title"])
        try:
            await drop_data_table(engine, table)  # 清理上次残留
            await create_data_table(engine, table)
            ing = Ingestor(engine)

            n1 = await ing.upsert(
                table,
                [_item("Book A", "10"), _item("Book B", "20")],
                batch_id=1,
                fingerprint_keys=["title"],
            )
            # 重复采集 Book A（改价 + 新批次）→ 指纹幂等：不新增行、刷新内容/批次
            n2 = await ing.upsert(table, [_item("Book A", "99")], batch_id=2, fingerprint_keys=["title"])

            async with engine.begin() as conn:
                count = (await conn.execute(select(func.count()).select_from(table))).scalar()
                row_a = (
                    await conn.execute(select(table.c.fields, table.c.batch_id).where(table.c["idx_title"] == "Book A"))
                ).first()

            assert n1 == 2
            assert n2 == 1
            assert count == 2  # 去重：仍 2 行（Book A 被 upsert 更新而非新增）
            assert row_a is not None
            assert row_a[0]["price"] == "99"  # 内容已更新
            assert row_a[1] == 2  # batch_id 已更新
        finally:
            await drop_data_table(engine, table)
            await engine.dispose()

    asyncio.run(main())
