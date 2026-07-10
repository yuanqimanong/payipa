"""M2 派发/回收 helper 集成测试（需 PG）：claim 只读 + mark_assigned 乐观锁 + 租约回收/放弃 +
断连回收 + 监控聚合（batch_progress / queue_depth）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import payipa_contracts as c
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Request
from payipa.db.settings import get_settings
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m2disp"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
        fingerprint=["title"],
    )


async def _req(pyp, req_id: int):
    async with pyp.begin() as conn:
        return (
            await conn.execute(
                select(Request.state, Request.attempt, Request.lease_until, Request.agent_id).where(
                    Request.id == req_id
                )
            )
        ).first()


async def _count_queued(pyp, batch_id: int) -> int:
    async with pyp.begin() as conn:
        return (
            await conn.execute(
                select(text("count(*)"))
                .select_from(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state == int(c.RequestState.QUEUED))
            )
        ).scalar()


async def _force(pyp, req_id: int, **vals) -> None:
    async with pyp.begin() as conn:
        await conn.execute(update(Request.__table__).where(Request.id == req_id).values(**vals))


def test_dispatch_and_reaper_helpers(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table(_UUID, indexed_fields=["title"])
        past = datetime.now(UTC) - timedelta(seconds=30)
        future = datetime.now(UTC) + timedelta(seconds=1800)
        try:
            await drop_data_table(dc, table)
            await run.ensure_data_table(dc, _UUID, ["title"])
            source_id, task_id = await run.setup_source(
                pyp,
                _UUID,
                "M2 Dispatch",
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed=True,
            )
            ptr = await RuleStore(pyp).put(source_id, _rule())
            batch_id, specs = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=_UUID, targets=["u1", "u2", "u3"], rule_ptr=ptr
            )
            r0, r1, r2 = (int(s.req_id) for s in specs)

            # 1) claim 只读：返回 3 条 TaskSpec，不改状态
            claimed = await run.claim_queued_for_dispatch(pyp, limit=10)
            assert {int(s.req_id) for s in claimed} == {r0, r1, r2}
            assert claimed[0].rule_ptr.content_hash == ptr.content_hash  # 能重建规则指针
            assert await _count_queued(pyp, batch_id) == 3

            # 2) mark_assigned 乐观锁：首次 1、二次 0；写入 agent_id/lease_until；claim 少一条
            assert await run.mark_assigned(pyp, r0, "agentA", future) == 1
            assert await run.mark_assigned(pyp, r0, "agentA", future) == 0
            row = await _req(pyp, r0)
            assert row.state == int(c.RequestState.ASSIGNED)
            assert row.agent_id == "agentA" and row.lease_until is not None
            assert len(await run.claim_queued_for_dispatch(pyp, limit=10)) == 2

            # 3) 租约到期回收：r0 lease 置过去 → 回 QUEUED（attempt+1、清 agent/lease）
            await _force(pyp, r0, lease_until=past)
            assert await run.requeue_expired_leases(pyp, max_attempt=3) == 1
            row = await _req(pyp, r0)
            assert row.state == int(c.RequestState.QUEUED)
            assert row.attempt == 1 and row.lease_until is None and row.agent_id is None

            # 4) 到达 max_attempt → 定格 NODE_LOST(-6)：r2 置 ASSIGNED、attempt=2、lease 过去
            await _force(pyp, r2, state=int(c.RequestState.ASSIGNED), attempt=2, lease_until=past, agent_id="agentA")
            assert await run.requeue_expired_leases(pyp, max_attempt=3) == 1
            row = await _req(pyp, r2)
            assert row.state == int(c.ErrorCode.NODE_LOST) and row.lease_until is None

            # 5) 断连回收：r1 占用给 agentB，再按 agent 回收 → 回 QUEUED（attempt+1）
            assert await run.mark_assigned(pyp, r1, "agentB", future) == 1
            assert await run.requeue_agent_inflight(pyp, "agentB", max_attempt=3) == 1
            row = await _req(pyp, r1)
            assert row.state == int(c.RequestState.QUEUED) and row.attempt == 1

            # 6) 监控聚合：r0/r1 QUEUED、r2 NODE_LOST → total=3, fail=1, running=2, pct=33.3
            prog = await run.batch_progress(pyp, batch_id)
            assert prog == {"total": 3, "ok": 0, "fail": 1, "running": 2, "pct": 33.3}
            # queue_depth 是全局（不按 batch 过滤），只断言至少含本批的 2 条排队
            assert (await run.queue_depth(pyp)).get("mid", 0) >= 2
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
