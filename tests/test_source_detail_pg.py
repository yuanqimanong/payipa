"""数据源详情页（§10.3）：单源全景 SSR——概览/任务/最近批次/规则版本一次渲染。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from payipa.db.pyp import Batch, Rule, Source, Task, User
from payipa.db.settings import get_settings
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_ADMIN = "srcdetail-probe-admin"
_PW = "abcd1234"
_UUID = "srcdetail_probe"
_NAME = "详情页测试源"
_HASH = "ab12cd34ef56" * 5 + "ab12"  # 64 位内容寻址 hash（页面显示前 12 位）


async def _seed() -> int:
    """建测试用户 + 源 + 任务 + 批次 + 规则；返回批次 id 供页面断言。"""
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            await conn.execute(
                pg_insert(User.__table__)
                .values(username=_ADMIN, password_hash=hash_password(_PW), status="active")
                .on_conflict_do_nothing(index_elements=["username"])
            )
            sid = (
                await conn.execute(
                    pg_insert(Source.__table__)
                    .values(
                        uuid=_UUID,
                        name=_NAME,
                        access_basis="public_policy",
                        access_reference="公开练习站",
                        access_confirmed_at=datetime.now(tz=UTC),
                    )
                    .returning(Source.id)
                )
            ).scalar_one()
            tid = (
                await conn.execute(
                    pg_insert(Task.__table__)
                    .values(source_id=sid, trigger_type="manual", priority="mid", params={"engine_hint": "http"})
                    .returning(Task.id)
                )
            ).scalar_one()
            bid = (
                await conn.execute(
                    pg_insert(Batch.__table__)
                    .values(task_id=tid, channel="prod", status="done", stats={})
                    .returning(Batch.id)
                )
            ).scalar_one()
            await conn.execute(
                pg_insert(Rule.__table__).values(
                    source_id=sid, version=1, content_hash=_HASH, status="active", spec={"fields": []}
                )
            )
        return int(bid)
    finally:
        await pyp.dispose()


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM batches WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u))"
                ),
                {"u": _UUID},
            )
            await conn.execute(
                text("DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)"), {"u": _UUID}
            )
            await conn.execute(
                text("DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)"), {"u": _UUID}
            )
            await conn.execute(text("DELETE FROM sources WHERE uuid=:u"), {"u": _UUID})
            await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _ADMIN})
    finally:
        await pyp.dispose()


def test_source_detail_page(require_pg: None) -> None:
    asyncio.run(_cleanup())
    bid = asyncio.run(_seed())
    try:
        with TestClient(create_app()) as client:  # 上下文管理器：单事件循环服务全部请求
            # 未登录 → 跳登录页
            assert client.get(f"/sources/{_UUID}", follow_redirects=False).status_code == 303

            client.get("/login")
            client.post(
                "/login",
                data={"username": _ADMIN, "password": _PW, "csrf_token": client.cookies.get("pyp_csrf")},
                follow_redirects=False,
            )
            r = client.get(f"/sources/{_UUID}")
            assert r.status_code == 200
            assert _NAME in r.text  # 概览：名称
            assert _UUID in r.text  # 概览：短码
            assert _HASH[:12] in r.text  # 规则版本：hash 前 12 位
            assert f"<code>{bid}</code>" in r.text  # 最近批次：批次 id
            assert "已确认" in r.text  # 访问状态徽标

            # 不存在的源 → 404
            assert client.get("/sources/no_such_code").status_code == 404

            # 列表页名称已变详情链接
            lst = client.get("/sources")
            assert f'href="/sources/{_UUID}"' in lst.text
    finally:
        asyncio.run(_cleanup())
