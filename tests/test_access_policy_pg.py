"""Data-source authorization and whole-source pause integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import payipa_contracts as c
import pytest
from payipa.crawl import run
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Request, Source
from payipa.db.settings import get_settings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "accesspolicy"


def _rule() -> c.RulePack:
    return c.RulePack(fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))])


def test_access_confirmation_pause_and_review(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            with pytest.raises(PermissionError):
                await run.setup_source(pyp, _UUID, "Unconfirmed")

            source_id, task_id = await run.setup_source(
                pyp,
                _UUID,
                "Approved",
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed=True,
            )
            ptr = await RuleStore(pyp).put(source_id, _rule())
            _, specs = await run.create_batch_with_requests(
                pyp,
                task_id=task_id,
                source_uuid=_UUID,
                targets=["https://example.test/a", "https://example.test/b", "https://example.test/c"],
                rule_ptr=ptr,
            )
            req0, req1, req2 = (int(spec.req_id) for spec in specs)
            lease = datetime.now(UTC) + timedelta(minutes=5)
            assert await run.mark_assigned(pyp, req0, "agent-a", lease) == 1
            assert await run.mark_assigned(pyp, req1, "agent-b", lease) == 1

            source_uuid, inflight, batch_ids = await run.pause_source_for_request(pyp, req0, "HTTP 403")
            assert source_uuid == _UUID
            assert inflight == [str(req1)]
            assert len(batch_ids) == 1

            async with pyp.connect() as conn:
                source = (
                    await conn.execute(
                        select(Source.access_confirmed_at, Source.paused_at, Source.pause_reason).where(
                            Source.uuid == _UUID
                        )
                    )
                ).one()
                states = dict(
                    (
                        await conn.execute(select(Request.id, Request.state).where(Request.id.in_([req0, req1, req2])))
                    ).all()
                )
            assert source.access_confirmed_at is not None and source.paused_at is not None
            assert source.pause_reason == "HTTP 403"
            assert states[req0] == int(c.ErrorCode.ACCESS_PAUSED)
            assert states[req1] == int(c.RequestState.ASSIGNED)
            assert states[req2] == int(c.ErrorCode.ACCESS_PAUSED)
            assert _UUID not in await run.source_rate_limits(pyp)
            assert not [spec for spec in await run.claim_queued_for_dispatch(pyp) if spec.source == _UUID]
            assert await run.requeue_agent_inflight(pyp, "agent-b") == 1
            async with pyp.connect() as conn:
                assert (await conn.execute(select(Request.state).where(Request.id == req1))).scalar_one() == int(
                    c.ErrorCode.ACCESS_PAUSED
                )

            with pytest.raises(PermissionError):
                await run.create_batch_with_requests(
                    pyp,
                    task_id=task_id,
                    source_uuid=_UUID,
                    targets=["https://example.test/new"],
                    rule_ptr=ptr,
                )

            assert await run.review_source_access(
                pyp,
                _UUID,
                access_basis="owned",
                access_reference="reviewed test fixture",
                approved=False,
                reason="依据材料不足，维持暂停",
            )
            async with pyp.connect() as conn:
                source = (
                    await conn.execute(
                        select(Source.access_confirmed_at, Source.paused_at, Source.pause_reason).where(
                            Source.uuid == _UUID
                        )
                    )
                ).one()
            assert source.access_confirmed_at is None and source.paused_at is not None
            assert source.pause_reason == "依据材料不足，维持暂停"
            with pytest.raises(PermissionError):
                await run.create_batch_with_requests(
                    pyp,
                    task_id=task_id,
                    source_uuid=_UUID,
                    targets=["https://example.test/rejected"],
                    rule_ptr=ptr,
                )

            assert await run.review_source_access(
                pyp,
                _UUID,
                access_basis="owned",
                access_reference="reviewed test fixture",
                approved=True,
            )
            _, resumed = await run.create_batch_with_requests(
                pyp,
                task_id=task_id,
                source_uuid=_UUID,
                targets=["https://example.test/resumed"],
                rule_ptr=ptr,
            )
            assert len(resumed) == 1
        finally:
            async with pyp.begin() as conn:
                for statement in (
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
            await pyp.dispose()

    asyncio.run(main())
