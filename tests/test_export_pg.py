"""数据导出（CSV/JSONL）+ 一键重跑 集成测试（需 PG）。"""

from __future__ import annotations

import asyncio
import json

import payipa_contracts as c
from fastapi.testclient import TestClient
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, create_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.settings import get_settings
from pyp_server.main import create_app
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "exp_src"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
        fingerprint=["title"],
    )


async def _seed() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    dc = create_async_engine(get_settings().async_url("data_center"))
    table = build_data_table(_UUID, [])
    try:
        await _cleanup_impl(pyp, dc)
        source_id, _task = await run.setup_source(
            pyp,
            _UUID,
            "Export Src",
            seed_urls=["https://x.test/seed"],
            access_basis="owned",
            access_reference="fixture",
            access_confirmed=True,
        )
        await RuleStore(pyp).put(source_id, _rule())
        await create_data_table(dc, table)
        async with dc.begin() as conn:
            for i, title in enumerate(["Alpha", "Bravo,X", "Δ 中文"]):  # 含逗号/中文，验证 CSV 转义 + BOM
                await conn.execute(pg_insert(table).values(data_fingerprint=f"fp{i}", state=3, fields={"title": title}))
    finally:
        await pyp.dispose()
        await dc.dispose()


async def _cleanup_impl(pyp, dc) -> None:
    await drop_data_table(dc, build_data_table(_UUID, []))
    async with pyp.begin() as conn:
        for sql in (
            "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
            "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
            "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
            "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
            "DELETE FROM sources WHERE uuid=:u",
        ):
            await conn.execute(text(sql), {"u": _UUID})


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    dc = create_async_engine(get_settings().async_url("data_center"))
    try:
        await _cleanup_impl(pyp, dc)
    finally:
        await pyp.dispose()
        await dc.dispose()


def test_export_and_rerun(require_pg: None) -> None:
    asyncio.run(_seed())
    try:
        with TestClient(create_app()) as client:
            # CSV：BOM + 表头含系统列与规则字段 + 逗号/中文正确
            csv_resp = client.get(f"/api/data/{_UUID}/export?fmt=csv")
            assert csv_resp.status_code == 200
            assert csv_resp.headers["content-disposition"] == f'attachment; filename="{_UUID}.csv"'
            body = csv_resp.content.decode("utf-8-sig")
            lines = [ln for ln in body.splitlines() if ln]
            assert lines[0].split(",") == ["id", "created_at", "state", "title"]
            assert len(lines) == 4  # 表头 + 3 行
            assert '"Bravo,X"' in body  # 含逗号字段被引号包裹
            assert "Δ 中文" in body

            # JSONL：每行一个对象，含 title
            jl = client.get(f"/api/data/{_UUID}/export?fmt=jsonl")
            assert jl.status_code == 200
            objs = [json.loads(ln) for ln in jl.content.decode().splitlines() if ln]
            assert len(objs) == 3
            assert {o["title"] for o in objs} == {"Alpha", "Bravo,X", "Δ 中文"}
            assert all("id" in o and "state" in o for o in objs)

            # 非法 fmt → 400
            assert client.get(f"/api/data/{_UUID}/export?fmt=xml").status_code == 400

            # 一键重跑（源已确认 + 有存档种子）→ 200 + batch_id
            rr = client.post(f"/api/sources/{_UUID}/rerun")
            assert rr.status_code == 200 and rr.json()["batch_id"] > 0
    finally:
        asyncio.run(_cleanup())


def test_rerun_unconfirmed_source_blocked(require_pg: None) -> None:
    """未确认访问依据的源不能重跑（访问策略闸门 → 409）。"""
    uuid = "exp_unconf"

    async def seed() -> int:
        # 直接落一个未确认访问依据的源（setup_source 会拒绝创建未确认源，故绕过它）
        from payipa.db.pyp import Source, Task

        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM sources WHERE uuid=:u"), {"u": uuid})
                sid = (
                    await conn.execute(
                        pg_insert(Source.__table__)
                        .values(uuid=uuid, name="Unconfirmed", connector_type="web")
                        .returning(Source.id)
                    )
                ).scalar_one()
                await conn.execute(
                    pg_insert(Task.__table__).values(
                        source_id=sid, trigger_type="manual", params={"seed_urls": ["https://x.test/s"]}
                    )
                )
            await RuleStore(pyp).put(sid, _rule())
            return sid
        finally:
            await pyp.dispose()

    async def cleanup() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)"), {"u": uuid}
                )
                await conn.execute(
                    text("DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)"), {"u": uuid}
                )
                await conn.execute(text("DELETE FROM sources WHERE uuid=:u"), {"u": uuid})
        finally:
            await pyp.dispose()

    asyncio.run(seed())
    try:
        with TestClient(create_app()) as client:
            assert client.post(f"/api/sources/{uuid}/rerun").status_code == 409
    finally:
        asyncio.run(cleanup())
