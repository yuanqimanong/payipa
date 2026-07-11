"""运行中心（P1-30 轻量版）：/api/ops/recent 登录保护 + running 置顶 + 字段形状。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.crawl import run
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from payipa.security.rbac import make_superuser, seed_default_rbac
from payipa_contracts import FieldRule, Locator, LocatorType, RulePack
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_ADMIN = "ops-probe-admin"
_PW = "abcd1234"
_UUID = "ops_probe"


async def _seed() -> int:
    """建管理员 + 一个带 running 批次的测试源；返回 batch_id。"""
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            await conn.execute(
                pg_insert(User.__table__)
                .values(username=_ADMIN, password_hash=hash_password(_PW), status="active")
                .on_conflict_do_nothing(index_elements=["username"])
            )
        await seed_default_rbac(pyp)
        await make_superuser(pyp, _ADMIN)
        source_id, task_id = await run.setup_source(
            pyp, _UUID, "运行中心探针", access_basis="owned", access_reference="test fixture", access_confirmed=True
        )
        rule = RulePack(
            fields=[FieldRule(name="t", locator=Locator(type=LocatorType.CSS, expr="h1"))], fingerprint=["t"]
        )
        ptr = await RuleStore(pyp).put(source_id, rule)
        batch_id, _ = await run.create_batch_with_requests(
            pyp, task_id=task_id, source_uuid=_UUID, targets=["https://x.com/ops"], rule_ptr=ptr
        )
        return batch_id
    finally:
        await pyp.dispose()


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            for sql in (
                "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                "DELETE FROM sources WHERE uuid=:u",
            ):
                await conn.execute(text(sql), {"u": _UUID})
            await conn.execute(
                text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username=:n)"), {"n": _ADMIN}
            )
            await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _ADMIN})
    finally:
        await pyp.dispose()


def test_ops_recent(require_pg: None) -> None:
    batch_id = asyncio.run(_seed())
    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/ops/recent").status_code == 401  # 未登录拒读

            client.get("/login")
            client.post(
                "/login",
                data={"username": _ADMIN, "password": _PW, "csrf_token": client.cookies.get("pyp_csrf")},
                follow_redirects=False,
            )
            rows = client.get("/api/ops/recent?limit=50").json()
            mine = [r for r in rows if r["batch_id"] == batch_id]
            assert mine and mine[0]["status"] == "running" and mine[0]["source"] == _UUID
            # running 批次置顶：首条若存在 running，其位置不晚于任何非 running
            statuses = [r["status"] for r in rows]
            if "running" in statuses:
                first_non_running = next((i for i, s in enumerate(statuses) if s != "running"), len(statuses))
                assert statuses.index("running") < max(first_non_running, 1)
    finally:
        asyncio.run(_cleanup())
