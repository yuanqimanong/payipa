"""M2 多波爬行 enqueue_discovered 集成测试（需 PG）：URL 指纹去重 + depth=父+1 + max_depth 上限 + 并入同批。"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Request
from payipa.db.settings import get_settings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m2crawl"


def _rule(max_depth: int | None) -> c.RulePack:
    crawl = c.CrawlRules(max_depth=max_depth) if max_depth is not None else None
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
        crawl=crawl,
        fingerprint=["title"],
    )


async def _rows(pyp, batch_id: int):
    async with pyp.begin() as conn:
        return (
            await conn.execute(
                select(Request.id, Request.depth, Request.target, Request.url_hash)
                .where(Request.batch_id == batch_id)
                .order_by(Request.id)
            )
        ).all()


def test_enqueue_discovered(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table(_UUID, indexed_fields=["title"])
        try:
            await drop_data_table(dc, table)
            await run.ensure_data_table(dc, _UUID, ["title"])
            src_id, task_id = await run.setup_source(
                pyp,
                _UUID,
                "M2 Crawl",
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed=True,
            )
            ptr = await RuleStore(pyp).put(src_id, _rule(max_depth=2))
            batch_id, specs = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=_UUID, targets=["https://x.com/p1"], rule_ptr=ptr
            )
            seed = int(specs[0].req_id)

            # 建批：seed 有 url_hash、depth=0
            rows = await _rows(pyp, batch_id)
            assert len(rows) == 1 and rows[0].depth == 0 and rows[0].url_hash
            # 反解源 + 每源限流表（本批 running）
            assert await run.source_of_request(pyp, seed) == _UUID
            assert (await run.source_rate_limits(pyp)).get(_UUID) == 10  # Source.rate_limit 默认 10

            # 发现 3 条（含 a 与 a#frag 同指纹）→ 去重后入 2 条，depth=1
            n = await run.enqueue_discovered(pyp, seed, ["https://x.com/a", "https://x.com/b", "https://x.com/a#frag"])
            assert n == 2
            # 相同链接再来一遍 → 批内唯一索引挡掉 → 0
            assert await run.enqueue_discovered(pyp, seed, ["https://x.com/a", "https://x.com/b"]) == 0
            depth1 = [r for r in await _rows(pyp, batch_id) if r.depth == 1]
            assert len(depth1) == 2
            assert "https://x.com/a#frag" not in {r.target for r in depth1}  # a#frag 与 a 同指纹、被去重

            # 从 depth1 再发现 → depth2 ≤ max_depth(2) → 入队
            assert await run.enqueue_discovered(pyp, depth1[0].id, ["https://x.com/c"]) == 1
            depth2 = [r for r in await _rows(pyp, batch_id) if r.depth == 2]
            assert len(depth2) == 1

            # 从 depth2 再发现 → depth3 > max_depth → 不入队
            assert await run.enqueue_discovered(pyp, depth2[0].id, ["https://x.com/d"]) == 0
            assert {r.depth for r in await _rows(pyp, batch_id)} == {0, 1, 2}  # 未越界

            # 无 crawl 规则 → max_depth=0 → 从 seed 不跟进（退化为单页）
            ptr0 = await RuleStore(pyp).put(src_id, _rule(max_depth=None))
            b2, s2 = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=_UUID, targets=["https://x.com/z"], rule_ptr=ptr0
            )
            assert await run.enqueue_discovered(pyp, int(s2[0].req_id), ["https://x.com/z2"]) == 0
        finally:
            async with pyp.begin() as conn:
                for sql in (
                    "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM sources WHERE uuid=:u",
                ):
                    await conn.execute(text(sql), {"u": _UUID})
            await drop_data_table(dc, table)
            await pyp.dispose()
            await dc.dispose()

    asyncio.run(main())
