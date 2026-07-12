"""Database-level test/prod output isolation."""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import DynamicSchema
from payipa.db.settings import get_settings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "channeliso"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[
            c.FieldRule(
                name="title",
                locator=c.Locator(type=c.LocatorType.CSS, expr="h1"),
                index=True,
            )
        ],
        fingerprint=["title"],
    )


def _result(batch_id: int, req_id: int, environment: str) -> c.ResultBatch:
    return c.ResultBatch(
        batch_id=str(batch_id),
        req_id=str(req_id),
        items=[c.Item(fields={"title": "same", "environment": environment})],
        summary=c.ExecSummary(elapsed_s=0.1, count_ok=1),
    )


def test_test_results_use_an_isolated_table(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        prod_table = build_data_table(_UUID, ["title"], c.Channel.PROD)
        test_table = build_data_table(_UUID, ["title"], c.Channel.TEST)
        try:
            await drop_data_table(dc, prod_table)
            await drop_data_table(dc, test_table)
            source_id, task_id = await run.setup_source(
                pyp,
                _UUID,
                "Channel isolation",
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed=True,
                raw_archive=True,
            )
            ptr = await RuleStore(pyp).put(source_id, _rule())
            await run.ensure_data_table(
                dc,
                _UUID,
                ["title"],
                engine_pyp=pyp,
                channel=c.Channel.PROD,
            )
            await run.ensure_data_table(
                dc,
                _UUID,
                ["title"],
                engine_pyp=pyp,
                channel=c.Channel.TEST,
            )

            prod_batch, prod_specs = await run.create_batch_with_requests(
                pyp,
                task_id=task_id,
                source_uuid=_UUID,
                targets=["https://example.com/prod"],
                rule_ptr=ptr,
                channel=c.Channel.PROD,
            )
            test_batch, test_specs = await run.create_batch_with_requests(
                pyp,
                task_id=task_id,
                source_uuid=_UUID,
                targets=["https://example.com/test"],
                rule_ptr=ptr,
                channel=c.Channel.TEST,
            )
            prod_spec, test_spec = prod_specs[0], test_specs[0]
            assert prod_spec.archive_raw is True
            assert test_spec.archive_raw is False

            for spec in (prod_spec, test_spec):
                req_id = int(spec.req_id)
                assert await run.mark_assigned(pyp, req_id, "agent-a", attempt=0, lease_s=60) == 1
                assert await run.mark_running(pyp, req_id, "agent-a", 0, lease_s=60) == 1

            assert (
                await run.commit_result(
                    pyp,
                    dc,
                    prod_table,
                    _result(prod_batch, int(prod_spec.req_id), "prod"),
                    fingerprint_keys=["title"],
                    agent_id="agent-a",
                )
            ).accepted
            assert (
                await run.commit_result(
                    pyp,
                    dc,
                    test_table,
                    _result(test_batch, int(test_spec.req_id), "test"),
                    fingerprint_keys=["title"],
                    agent_id="agent-a",
                )
            ).accepted

            async with dc.connect() as conn:
                prod_rows = (await conn.execute(select(prod_table.c.fields))).scalars().all()
                test_rows = (await conn.execute(select(test_table.c.fields))).scalars().all()
            assert prod_rows == [{"title": "same", "environment": "prod"}]
            assert test_rows == [{"title": "same", "environment": "test"}]

            prod_context = await run.resolve_ingest_context(pyp, int(prod_spec.req_id))
            test_context = await run.resolve_ingest_context(pyp, int(test_spec.req_id))
            assert prod_context[3] is c.Channel.PROD
            assert test_context[3] is c.Channel.TEST

            async with pyp.connect() as conn:
                ledger = (
                    await conn.execute(
                        select(DynamicSchema.channel, DynamicSchema.table_name)
                        .where(DynamicSchema.kind == "data", DynamicSchema.object_code == _UUID)
                        .order_by(DynamicSchema.channel)
                    )
                ).all()
            assert ledger == [("prod", prod_table.name), ("test", test_table.name)]
        finally:
            async with pyp.begin() as conn:
                for sql in (
                    "DELETE FROM task_events WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM dynamic_schemas WHERE object_code=:u",
                    "DELETE FROM sources WHERE uuid=:u",
                ):
                    await conn.execute(text(sql), {"u": _UUID})
            await drop_data_table(dc, prod_table)
            await drop_data_table(dc, test_table)
            await pyp.dispose()
            await dc.dispose()

    asyncio.run(main())
