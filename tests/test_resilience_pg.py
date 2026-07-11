"""采集韧性闭环：持久化 Retry-After、源级冷却、到期恢复与重试耗尽。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import payipa_contracts as c
from payipa.crawl import run
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Request, Source
from payipa.db.settings import get_settings
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "resilience"


async def _purge(pyp) -> None:
    async with pyp.begin() as conn:
        for statement in (
            "DELETE FROM task_events WHERE batch_id IN "
            "(SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id "
            "JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",
            "DELETE FROM requests WHERE batch_id IN "
            "(SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id "
            "JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",
            "DELETE FROM batches WHERE task_id IN "
            "(SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",
            "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
            "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
            "DELETE FROM sources WHERE uuid=:u",
        ):
            await conn.execute(text(statement), {"u": _UUID})


def test_retry_after_cooldown_and_exhaustion(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await _purge(pyp)
            source_id, task_id = await run.setup_source(
                pyp,
                _UUID,
                "Resilience",
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed=True,
                engine_hint=c.EngineHint.BROWSER,
                rate_limit=5,
                retry=3,
                timeout=45,
                raw_archive=True,
            )
            rule = c.RulePack(fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))])
            ptr = await RuleStore(pyp).put(source_id, rule)
            _, specs = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=_UUID, targets=["https://example.test/a"], rule_ptr=ptr
            )
            req_id = int(specs[0].req_id)
            lease = datetime.now(UTC) + timedelta(minutes=5)
            assert await run.mark_assigned(pyp, req_id, "agent-a", lease) == 1

            source, requeued = await run.defer_request_for_retry(
                pyp,
                req_id,
                int(c.ErrorCode.THROTTLED),
                retry_after_s=60,
                response_status=429,
                reason_code="rate_limited",
                message="target requested a slower rate",
                max_attempt=3,
            )
            assert source == _UUID and requeued is True
            async with pyp.connect() as conn:
                req = (
                    await conn.execute(
                        select(
                            Request.state,
                            Request.attempt,
                            Request.not_before,
                            Request.response_status,
                            Request.reason_code,
                        ).where(Request.id == req_id)
                    )
                ).one()
                src = (
                    await conn.execute(
                        select(
                            Source.cooldown_until,
                            Source.cooldown_reason,
                            Source.consecutive_failures,
                            Source.last_status_code,
                        ).where(Source.uuid == _UUID)
                    )
                ).one()
            assert req.state == int(c.RequestState.QUEUED) and req.attempt == 1
            assert req.not_before is not None and req.response_status == 429 and req.reason_code == "rate_limited"
            assert src.cooldown_until is not None and src.cooldown_reason == "rate_limited"
            assert src.consecutive_failures == 1 and src.last_status_code == 429
            assert not [s for s in await run.claim_queued_for_dispatch(pyp) if s.source == _UUID]

            # 重复/迟到回报不得再次消耗预算或延长状态机。
            _, duplicate_requeued = await run.defer_request_for_retry(
                pyp, req_id, int(c.ErrorCode.THROTTLED), retry_after_s=300, response_status=429
            )
            assert duplicate_requeued is True
            async with pyp.connect() as conn:
                assert (await conn.execute(select(Request.attempt).where(Request.id == req_id))).scalar_one() == 1

            past = datetime.now(UTC) - timedelta(seconds=1)
            async with pyp.begin() as conn:
                await conn.execute(update(Request.__table__).where(Request.id == req_id).values(not_before=past))
                await conn.execute(update(Source.__table__).where(Source.uuid == _UUID).values(cooldown_until=past))
            ready = [s for s in await run.claim_queued_for_dispatch(pyp) if int(s.req_id) == req_id]
            assert ready
            assert ready[0].engine_hint is c.EngineHint.BROWSER
            assert ready[0].timeout_s == 45 and ready[0].archive_raw is True

            # attempt=1 -> 第二次仍可重排；attempt=2 -> 第三次达到上限定格 UPSTREAM。
            assert await run.mark_assigned(pyp, req_id, "agent-a", lease) == 1
            _, requeued = await run.defer_request_for_retry(
                pyp, req_id, int(c.ErrorCode.UPSTREAM), retry_after_s=1, response_status=503, max_attempt=3
            )
            assert requeued is True
            async with pyp.begin() as conn:
                await conn.execute(update(Request.__table__).where(Request.id == req_id).values(not_before=past))
                await conn.execute(update(Source.__table__).where(Source.uuid == _UUID).values(cooldown_until=past))
            assert await run.mark_assigned(pyp, req_id, "agent-a", lease) == 1
            _, requeued = await run.defer_request_for_retry(
                pyp, req_id, int(c.ErrorCode.UPSTREAM), retry_after_s=1, response_status=503, max_attempt=3
            )
            assert requeued is False
            async with pyp.connect() as conn:
                state = (await conn.execute(select(Request.state).where(Request.id == req_id))).scalar_one()
            assert state == int(c.ErrorCode.UPSTREAM)
        finally:
            await _purge(pyp)
            await pyp.dispose()

    asyncio.run(main())
