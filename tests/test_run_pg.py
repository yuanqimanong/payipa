"""M1-4 集成测试（需 PG）：RuleStore 内容寻址 + 批次/请求创建 + 结果入库 + 状态推进 + 批次收尾。"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore, content_hash
from payipa.db.pyp import Batch, Request, Source, Task
from payipa.db.settings import get_settings
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m1run"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
        fingerprint=["title"],
    )


def _result(batch_id: int, req_id: int) -> c.ResultBatch:
    return c.ResultBatch(
        batch_id=str(batch_id),
        req_id=str(req_id),
        items=[
            c.Item(
                fields={"title": "Hello"},
                field_meta={"title": c.FieldMeta(normalized_value="Hello", confidence=1.0)},
            )
        ],
        summary=c.ExecSummary(elapsed_s=0.1, count_ok=1),
    )


def test_run_end_to_end_logic(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table(_UUID, indexed_fields=["title"])
        try:
            await drop_data_table(dc, table)
            await run.ensure_data_table(dc, _UUID, ["title"])

            async with pyp.begin() as conn:
                src_id = (
                    await conn.execute(
                        pg_insert(Source.__table__)
                        .values(
                            uuid=_UUID,
                            name="M1 Run",
                            connector_type="web",
                            access_basis="owned",
                            access_reference="test fixture",
                            access_confirmed_at=func.now(),
                        )
                        .returning(Source.id)
                    )
                ).scalar_one()
                task_id = (
                    await conn.execute(
                        pg_insert(Task.__table__).values(source_id=src_id, trigger_type="manual").returning(Task.id)
                    )
                ).scalar_one()

            # 内容寻址：put → get_by_hash roundtrip
            store = RuleStore(pyp)
            ptr = await store.put(src_id, _rule())
            assert ptr.content_hash == content_hash(_rule())
            fetched = await store.get_by_hash(ptr.content_hash)
            assert fetched is not None
            assert fetched.fields[0].name == "title"

            # 批次 + 请求
            batch_id, specs = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=_UUID, targets=["https://x.com/a"], rule_ptr=ptr
            )
            assert len(specs) == 1
            req_id = int(specs[0].req_id)
            assert specs[0].rule_ptr.content_hash == ptr.content_hash
            # 请求未完成 → 批次不收尾
            assert await run.finalize_batch_if_done(pyp, batch_id) is False

            # 结果入库（先数据后状态）
            written = await run.handle_result(pyp, dc, table, _result(batch_id, req_id), fingerprint_keys=["title"])
            assert written == 1

            async with pyp.begin() as conn:  # request 状态在 pyp 库
                state = (await conn.execute(select(Request.state).where(Request.id == req_id))).scalar()
            async with dc.begin() as conn:  # 数据行在 data_center 库
                rows = (await conn.execute(select(func.count()).select_from(table))).scalar()
            assert state == int(c.RequestState.SUCCESS)
            assert rows == 1

            # 全部完成 → 批次 done
            assert await run.finalize_batch_if_done(pyp, batch_id) is True
            async with pyp.begin() as conn:
                status = (await conn.execute(select(Batch.status).where(Batch.id == batch_id))).scalar()
            assert status == "done"
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
