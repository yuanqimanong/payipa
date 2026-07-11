"""M2-A 集成测试（需 PG）：优先级排序派发 + 批次取消(清排队/在途标记/canceling→canceled) + cron 到点调度。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import payipa_contracts as c
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Batch, Schedule, Task
from payipa.db.settings import get_settings
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m2sc"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
        fingerprint=["title"],
    )


async def _task(pyp, source_id: int, priority: str) -> int:
    async with pyp.begin() as conn:
        return (
            await conn.execute(
                pg_insert(Task.__table__)
                .values(source_id=source_id, trigger_type="manual", priority=priority)
                .returning(Task.id)
            )
        ).scalar_one()


def test_priority_cancel_schedule(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table(_UUID, indexed_fields=[])
        try:
            await drop_data_table(dc, table)
            await run.ensure_data_table(dc, _UUID, [])
            source_id, task0 = await run.setup_source(
                pyp,
                _UUID,
                "M2 SC",
                seed_urls=["https://x.com/seed"],
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed=True,
            )
            ptr = await RuleStore(pyp).put(source_id, _rule())

            # ── 优先级排序：按 low→high→mid 顺序建（created_at 递增），claim 应回 high→mid→low ──
            reqs = {}
            for prio, url in [("low", "u-lo"), ("high", "u-hi"), ("mid", "u-mid")]:
                tid = await _task(pyp, source_id, prio)
                _, specs = await run.create_batch_with_requests(
                    pyp, task_id=tid, source_uuid=_UUID, targets=[f"https://x.com/{url}"], rule_ptr=ptr
                )
                reqs[prio] = int(specs[0].req_id)
            claimed = await run.claim_queued_for_dispatch(pyp, limit=100)
            ours = [s for s in claimed if int(s.req_id) in reqs.values()]
            assert [s.priority for s in ours] == [c.Priority.HIGH, c.Priority.MID, c.Priority.LOW]

            # ── 取消：3 条排队，先占 1 条为 ASSIGNED，取消后 2 条就地 CANCELED、1 条在途待收尾、批次 canceling ──
            tc = await _task(pyp, source_id, "mid")
            bc, specs = await run.create_batch_with_requests(
                pyp,
                task_id=tc,
                source_uuid=_UUID,
                targets=["https://x.com/c1", "https://x.com/c2", "https://x.com/c3"],
                rule_ptr=ptr,
            )
            inflight_seed = int(specs[0].req_id)
            assert await run.mark_assigned(pyp, inflight_seed, "agX", datetime.now(UTC) + timedelta(seconds=600)) == 1
            inflight_ids, queued_ids = await run.cancel_batch(pyp, bc)
            assert len(queued_ids) == 2 and inflight_ids == [str(inflight_seed)]
            async with pyp.begin() as conn:
                status = (await conn.execute(select(Batch.status).where(Batch.id == bc))).scalar()
            assert status == "canceling"
            # sweep 此刻不收口（还有在途）
            assert await run.sweep_canceling_batches(pyp) == 0
            # agent 回报 CANCELED（模拟）→ sweep 收口为 canceled
            await run.set_request_state(pyp, inflight_seed, int(c.RequestState.CANCELED))
            assert await run.sweep_canceling_batches(pyp) >= 1
            async with pyp.begin() as conn:
                status = (await conn.execute(select(Batch.status).where(Batch.id == bc))).scalar()
            assert status == "canceled"

            # ── cron 到点调度：next_run_at=NULL ⇒ 到点；带 task0 的存档种子；advance 后不再到点 ──
            async with pyp.begin() as conn:
                sched_id = (
                    await conn.execute(
                        pg_insert(Schedule.__table__)
                        .values(task_id=task0, cron_expr="*/5 * * * *", next_run_at=None, enabled=True)
                        .returning(Schedule.id)
                    )
                ).scalar_one()
            due = await run.due_schedules(pyp)
            mine = [d for d in due if d[0] == sched_id]
            assert mine and mine[0][3] == _UUID and mine[0][4] == ["https://x.com/seed"]

            # ── DB-010：claim_schedule 对同一到期时间点只有一个赢家；认领后不再到期 ──
            future = datetime.now(UTC) + timedelta(hours=1)
            first = await run.claim_schedule(pyp, sched_id, future)
            second = await run.claim_schedule(pyp, sched_id, future)
            assert first is True and second is False
            assert not [d for d in await run.due_schedules(pyp) if d[0] == sched_id]

            # 停用后即使到期也不能认领
            async with pyp.begin() as conn:
                await conn.execute(text("UPDATE schedules SET next_run_at=NULL WHERE id=:i"), {"i": sched_id})
            await run.disable_schedule(pyp, sched_id)
            assert await run.claim_schedule(pyp, sched_id, future) is False
        finally:
            async with pyp.begin() as conn:
                for sql in (
                    "DELETE FROM schedules WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
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
