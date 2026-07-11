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


def test_claim_fairness(require_pg: None) -> None:
    """P0-11 验收：能力过滤——队头缺能力的请求不占窗口；源轮转——单源积压不霸占窗口。"""
    ua, ub = "m2faira", "m2fairb"
    ga, gb = "m2fair-gpu", "m2fair-web"

    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            ids = {}
            for u, g, n in ((ua, ga, 10), (ub, gb, 2)):
                source_id, task_id = await run.setup_source(
                    pyp, u, f"公平性 {u}", access_basis="owned", access_reference="test fixture", access_confirmed=True
                )
                async with pyp.begin() as conn:
                    await conn.execute(text("UPDATE sources SET agent_group=:g WHERE uuid=:u"), {"g": g, "u": u})
                ptr = await RuleStore(pyp).put(source_id, _rule())
                _, specs = await run.create_batch_with_requests(
                    pyp,
                    task_id=task_id,
                    source_uuid=u,
                    targets=[f"https://x.com/{u}/{i}" for i in range(n)],
                    rule_ptr=ptr,
                )
                ids[u] = {int(s.req_id) for s in specs}

            # 能力过滤：只有 B 组节点空闲 → A 的 10 条积压不再堵住窗口，B 全部可见
            claimed = await run.claim_queued_for_dispatch(pyp, limit=5, caps={gb: {"http"}})
            got = {int(s.req_id) for s in claimed}
            assert got & ids[ub] == ids[ub], "B 组请求应全部进入窗口"
            assert not (got & ids[ua]), "A 组（无空闲节点）请求不应占窗口"

            # 源轮转：两组都有空闲节点、窗口=4 → A/B 各得 2 条，单源积压不能霸占
            claimed = await run.claim_queued_for_dispatch(pyp, limit=4, caps={ga: {"http"}, gb: {"http"}})
            srcs = [s.source for s in claimed if int(s.req_id) in ids[ua] | ids[ub]]
            assert srcs.count(ua) == 2 and srcs.count(ub) == 2, f"应各 2 条，实际 {srcs}"

            # 无空闲节点 → 直接空窗，不扫库
            assert await run.claim_queued_for_dispatch(pyp, limit=5, caps={}) == []
        finally:
            async with pyp.begin() as conn:
                for u in (ua, ub):
                    for sql in (
                        "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                        "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                        "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                        "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                        "DELETE FROM sources WHERE uuid=:u",
                    ):
                        await conn.execute(text(sql), {"u": u})
            await pyp.dispose()

    asyncio.run(main())


def test_ack_and_attempt_fencing(require_pg: None) -> None:
    """P0-10 验收：mark_assigned 代次守卫、mark_running ACK 展租约、迟到结果/状态不覆盖当前状态。"""
    uuid = "m2fence"

    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        future = datetime.now(UTC) + timedelta(seconds=600)
        try:
            source_id, task_id = await run.setup_source(
                pyp, uuid, "Fencing", access_basis="owned", access_reference="test fixture", access_confirmed=True
            )
            ptr = await RuleStore(pyp).put(source_id, _rule())
            _, specs = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=uuid, targets=["https://x.com/f1"], rule_ptr=ptr
            )
            rid = int(specs[0].req_id)
            assert specs[0].attempt == 0  # claim 出的 TaskSpec 带代次

            # 代次守卫：错代次抢不到；对代次可抢
            assert await run.mark_assigned(pyp, rid, "agA", future, attempt=99) == 0
            assert await run.mark_assigned(pyp, rid, "agA", future, attempt=0) == 1

            # ACK：归属+代次吻合 → RUNNING；错 agent / 错代次 → 0
            assert await run.mark_running(pyp, rid, "agB", 0, lease_s=600) == 0
            assert await run.mark_running(pyp, rid, "agA", 1, lease_s=600) == 0
            assert await run.mark_running(pyp, rid, "agA", 0, lease_s=600) == 1
            row = await _req(pyp, rid)
            assert row.state == int(c.RequestState.RUNNING)

            # 迟到状态回报：错 agent / 错代次 → 不改状态
            assert await run.set_request_state(pyp, rid, int(c.RequestState.CANCELED), agent_id="agB", attempt=0) == 0
            assert await run.set_request_state(pyp, rid, int(c.RequestState.CANCELED), agent_id="agA", attempt=7) == 0
            assert (await _req(pyp, rid)).state == int(c.RequestState.RUNNING)

            # fencing 预检：越权/错代次/终态均 False，正主 True
            assert await run.fence_ok(pyp, rid, "agB", 0) is False
            assert await run.fence_ok(pyp, rid, "agA", 3) is False
            assert await run.fence_ok(pyp, rid, "agA", 0) is True

            # 正主正确回报 → 生效
            assert await run.set_request_state(pyp, rid, int(c.RequestState.CANCELED), agent_id="agA", attempt=0) == 1
            assert (await _req(pyp, rid)).state == int(c.RequestState.CANCELED)
            # 终态后一切回报都是 stale——即使归属与代次全对也不得覆盖终态
            assert await run.fence_ok(pyp, rid, "agA", 0) is False
            assert await run.set_request_state(pyp, rid, int(c.ErrorCode.SOFT_FAIL), agent_id="agA", attempt=0) == 0
            assert (await _req(pyp, rid)).state == int(c.RequestState.CANCELED)
        finally:
            async with pyp.begin() as conn:
                for sql in (
                    "DELETE FROM task_events WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM sources WHERE uuid=:u",
                ):
                    await conn.execute(text(sql), {"u": uuid})
            await pyp.dispose()

    asyncio.run(main())
