"""M1-4 集成测试（需 PG）：RuleStore 内容寻址 + 批次/请求创建 + 结果入库 + 状态推进 + 批次收尾。"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
import pytest
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
        crawl=c.CrawlRules(max_depth=1),
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

            # 上次测试若在清理前中断，先按 FK 顺序清残留，保证本用例可重复执行。
            async with pyp.begin() as conn:
                for sql in (
                    "DELETE FROM task_events WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM sources WHERE uuid=:u",
                ):
                    await conn.execute(text(sql), {"u": _UUID})

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

            # 结果提交的 fencing 是唯一权威门：错 attempt 的迟到结果连 discovered 子请求也不能留下。
            assert await run.mark_assigned(pyp, req_id, "agent-a", attempt=0, lease_s=60) == 1
            assert await run.mark_running(pyp, req_id, "agent-a", 0, lease_s=60) == 1
            stale = _result(batch_id, req_id).model_copy(
                update={"attempt": 1, "discovered": ["https://x.com/must-not-be-enqueued"]}
            )
            rejected = await run.commit_result(pyp, dc, table, stale, fingerprint_keys=["title"], agent_id="agent-a")
            assert rejected.accepted is False
            async with pyp.begin() as conn:
                assert (
                    await conn.execute(
                        select(func.count()).select_from(Request.__table__).where(Request.batch_id == batch_id)
                    )
                ).scalar() == 1

            # 正确代次结果入库（先数据后状态）
            written = await run.handle_result(
                pyp,
                dc,
                table,
                _result(batch_id, req_id),
                fingerprint_keys=["title"],
                agent_id="agent-a",
            )
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
                    "DELETE FROM task_events WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
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


def test_rule_dedup_scoped_to_source(require_pg: None) -> None:
    """DB-001 验收：两个源提交相同 spec 各得其行；重复 put 幂等；请求 rule_id 指向本源规则。"""
    uuids = ("m1rula", "m1rulb")

    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            src_ids: list[int] = []
            task_ids: list[int] = []
            async with pyp.begin() as conn:
                for u in uuids:
                    sid = (
                        await conn.execute(
                            pg_insert(Source.__table__)
                            .values(
                                uuid=u,
                                name=f"规则归属 {u}",
                                connector_type="web",
                                access_basis="owned",
                                access_reference="test fixture",
                                access_confirmed_at=func.now(),
                            )
                            .returning(Source.id)
                        )
                    ).scalar_one()
                    tid = (
                        await conn.execute(
                            pg_insert(Task.__table__).values(source_id=sid, trigger_type="manual").returning(Task.id)
                        )
                    ).scalar_one()
                    src_ids.append(sid)
                    task_ids.append(tid)

            store = RuleStore(pyp)
            ptr_a = await store.put(src_ids[0], _rule())
            ptr_b = await store.put(src_ids[1], _rule())
            # 相同 spec、不同源 → 各自成行、各自 version=1
            assert ptr_a.content_hash == ptr_b.content_hash
            assert ptr_a.rule_id != ptr_b.rule_id
            assert ptr_a.version == 1
            assert ptr_b.version == 1
            # 同源重复 put → 幂等返回同一行
            again = await store.put(src_ids[0], _rule())
            assert again.rule_id == ptr_a.rule_id
            assert again.version == ptr_a.version

            # 跨源规则指针必须在建批事务内拒绝，不能只依赖调用方自觉。
            with pytest.raises(PermissionError, match="不属于"):
                await run.create_batch_with_requests(
                    pyp,
                    task_id=task_ids[0],
                    source_uuid=uuids[0],
                    targets=["https://x.com/cross-source"],
                    rule_ptr=ptr_b,
                )

            draft_pack = c.RulePack(
                fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h2"))]
            )
            draft_ptr = await store.put(src_ids[0], draft_pack, status="draft")
            with pytest.raises(PermissionError, match="active"):
                await run.create_batch_with_requests(
                    pyp,
                    task_id=task_ids[0],
                    source_uuid=uuids[0],
                    targets=["https://x.com/draft-prod"],
                    rule_ptr=draft_ptr,
                )
            _, draft_specs = await run.create_batch_with_requests(
                pyp,
                task_id=task_ids[0],
                source_uuid=uuids[0],
                targets=["https://x.com/draft-test"],
                rule_ptr=draft_ptr,
                channel=c.Channel.TEST,
            )
            assert draft_specs[0].channel is c.Channel.TEST

            # 请求写入权威 rule_id，且指向本源的规则行
            for tid, u, ptr in ((task_ids[0], uuids[0], ptr_a), (task_ids[1], uuids[1], ptr_b)):
                _, specs = await run.create_batch_with_requests(
                    pyp, task_id=tid, source_uuid=u, targets=[f"https://x.com/{u}"], rule_ptr=ptr
                )
                assert specs[0].rule_ptr.rule_id == ptr.rule_id
                async with pyp.begin() as conn:
                    rid = (
                        await conn.execute(select(Request.rule_id).where(Request.id == int(specs[0].req_id)))
                    ).scalar()
                assert rid == int(ptr.rule_id)
        finally:
            async with pyp.begin() as conn:
                for u in uuids:
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
