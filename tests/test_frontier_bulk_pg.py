"""续爬大批量写入的真实 PG 回归：有界往返、去重和整批原子性。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import payipa_contracts as c
import pytest
from payipa.crawl import frontier, run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Request
from payipa.db.settings import get_settings
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import create_async_engine


@asynccontextmanager
async def _running_batch():
    source = f"bulkfrontier{uuid4().hex[:16]}"
    pyp = create_async_engine(get_settings().async_url("pyp"))
    dc = create_async_engine(get_settings().async_url("data_center"))
    table = build_data_table(source, indexed_fields=["title"])
    try:
        await run.ensure_data_table(dc, source, ["title"])
        source_id, task_id = await run.setup_source(
            pyp,
            source,
            "Bulk frontier fixture",
            access_basis="owned",
            access_reference="isolated test fixture",
            access_confirmed=True,
        )
        pack = c.RulePack(
            fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
            crawl=c.CrawlRules(max_depth=1),
            fingerprint=["title"],
        )
        pointer = await RuleStore(pyp).put(source_id, pack)
        batch_id, specs = await run.create_batch_with_requests(
            pyp,
            task_id=task_id,
            source_uuid=source,
            targets=["https://example.test/seed"],
            rule_ptr=pointer,
        )
        yield pyp, batch_id, int(specs[0].req_id), pointer
    finally:
        async with pyp.begin() as conn:
            for statement in (
                "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                "DELETE FROM sources WHERE uuid=:u",
            ):
                await conn.execute(text(statement), {"u": source})
        await drop_data_table(dc, table)
        await pyp.dispose()
        await dc.dispose()


def test_large_frontier_uses_bounded_writes_and_preserves_conflict_counts(require_pg: None) -> None:
    async def main() -> None:
        async with _running_batch() as (engine, batch_id, parent, pointer):
            writes = 0

            def count_writes(conn, cursor, statement, parameters, context, executemany):
                nonlocal writes
                if statement.startswith("INSERT INTO requests "):
                    writes += 1

            targets = [f"https://example.test/item/{index}" for index in range(4001)]
            targets += [targets[0] + "#again", targets[500] + "#again", "https://example.test/seed"]
            event.listen(engine.sync_engine, "before_cursor_execute", count_writes)
            try:
                assert await frontier.enqueue_discovered(engine, parent, targets) == 4001
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count_writes)
            # 大页面不应变成每个 URL 一次数据库往返，也不能一次提交超过驱动的参数上限。
            assert 1 < writes <= 9, f"4001 URLs required {writes} INSERT statements"
            assert await frontier.enqueue_discovered(engine, parent, targets) == 0
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        select(Request.target, Request.depth, Request.rule_id, Request.rule_hash, Request.rule_version)
                        .where(Request.batch_id == batch_id, Request.id != parent)
                        .order_by(Request.id)
                    )
                ).all()
            assert [row.target for row in rows] == targets[:4001]
            assert all(
                row.depth == 1
                and row.rule_id == int(pointer.rule_id)
                and row.rule_hash == pointer.content_hash
                and row.rule_version == pointer.version
                for row in rows
            )

    asyncio.run(main())


def test_frontier_rolls_back_all_chunks_when_later_write_fails(require_pg: None) -> None:
    async def main() -> None:
        async with _running_batch() as (engine, batch_id, parent, _):
            writes = 0

            def fail_later_write(conn, cursor, statement, parameters, context, executemany):
                nonlocal writes
                if statement.startswith("INSERT INTO requests "):
                    writes += 1
                    if writes == 2:
                        raise RuntimeError("injected frontier write failure")

            event.listen(engine.sync_engine, "before_cursor_execute", fail_later_write)
            try:
                with pytest.raises(RuntimeError, match="injected frontier write failure"):
                    await frontier.enqueue_discovered(
                        engine, parent, [f"https://example.test/item/{index}" for index in range(1201)]
                    )
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", fail_later_write)
            async with engine.connect() as conn:
                remaining = (await conn.execute(select(Request.id).where(Request.batch_id == batch_id))).scalars().all()
            assert remaining == [parent]

    asyncio.run(main())
