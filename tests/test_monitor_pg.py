"""M5 监控聚合集成测试（需 PG）：node_metrics / source_health / system_overview + HTTP 端点。

构造一个源的一批 4 个请求：2 成功（含解析计数：一条全字段 ok、一条空白 blank）、1 PARSE_FAIL、1 TIMEOUT；
注册一个 agent 并把成功请求归到它名下，断言聚合出的成败率、数据质量、错误码分布正确。
"""

from __future__ import annotations

import asyncio

import payipa_contracts as c
from fastapi.testclient import TestClient
from payipa.crawl import run
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db.pyp import Request, User
from payipa.db.settings import get_settings
from payipa.monitor import node_metrics, source_health, system_overview
from payipa.security.rbac import make_superuser, seed_default_rbac
from pyp_server.auth import COOKIE_NAME, create_session
from pyp_server.main import app
from pyp_server.settings import get_server_settings
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m5mon"
_AGENT = "mon-agent-1"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
        fingerprint=["title"],
    )


def _result(batch_id: int, req_id: int, *, ok: int, blank: int, elapsed: float) -> c.ResultBatch:
    items = [c.Item(fields={"title": f"t{i}"}) for i in range(ok)] + [c.Item(fields={}) for _ in range(blank)]
    return c.ResultBatch(
        batch_id=str(batch_id),
        req_id=str(req_id),
        items=items,
        summary=c.ExecSummary(elapsed_s=elapsed, count_ok=ok, count_fail=0, count_blank=blank),
    )


async def _purge(pyp, dc) -> None:
    """彻底清本源痕迹（requests→batches→tasks→sources + agent + 动态表），使断言不受历史运行污染。"""
    async with pyp.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b "
                "JOIN tasks t ON b.task_id = t.id JOIN sources s ON t.source_id = s.id WHERE s.uuid = :u)"
            ),
            {"u": _UUID},
        )
        await conn.execute(
            text(
                "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t "
                "JOIN sources s ON t.source_id = s.id WHERE s.uuid = :u)"
            ),
            {"u": _UUID},
        )
        await conn.execute(
            text("DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid = :u)"), {"u": _UUID}
        )
        await conn.execute(
            text("DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid = :u)"), {"u": _UUID}
        )
        await conn.execute(text("DELETE FROM sources WHERE uuid = :u"), {"u": _UUID})
        await conn.execute(text("DELETE FROM agents WHERE agent_id = :a"), {"a": _AGENT})
    await drop_data_table(dc, build_data_table(_UUID, ["title"]))


def test_monitor_aggregation(require_pg: None) -> None:
    async def main() -> dict:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        try:
            await _purge(pyp, dc)  # 起点先清历史（防上次失败残留污染精确断言）
            table = await run.ensure_data_table(dc, _UUID, ["title"])
            source_id, task_id = await run.setup_source(pyp, _UUID, "M5 Monitor")
            ptr = await RuleStore(pyp).put(source_id, _rule())
            batch_id, specs = await run.create_batch_with_requests(
                pyp, task_id=task_id, source_uuid=_UUID, targets=["u1", "u2", "u3", "u4"], rule_ptr=ptr
            )
            r0, r1, r2, r3 = (int(s.req_id) for s in specs)

            # 注册 agent，并把两条成功请求归属它（node_metrics 按 agent_id 汇总）
            await run.register_agent(pyp, _AGENT, hostname="h", slot_n=4, capabilities={}, node_token_hash="x")
            async with pyp.begin() as conn:
                await conn.execute(update(Request.__table__).where(Request.id.in_([r0, r1])).values(agent_id=_AGENT))

            # r0 成功：3 ok / 0 blank；r1 成功：0 ok / 1 blank
            # （单条空白：多条全空 item 指纹相同会在批内 upsert 冲突，属 Ingestor 既有行为、非本切片范畴）
            fk = ["title"]
            await run.handle_result(
                pyp, dc, table, _result(batch_id, r0, ok=3, blank=0, elapsed=1.0), fingerprint_keys=fk
            )
            await run.handle_result(
                pyp, dc, table, _result(batch_id, r1, ok=0, blank=1, elapsed=0.5), fingerprint_keys=fk
            )
            # r2 PARSE_FAIL(-5)，r3 TIMEOUT(-2)
            await run.set_request_state(pyp, r2, int(c.ErrorCode.PARSE_FAIL))
            await run.set_request_state(pyp, r3, int(c.ErrorCode.TIMEOUT))

            nodes = await node_metrics(pyp, live_slots={_AGENT: 1})
            sources = await source_health(pyp)
            overview = await system_overview(pyp)
            return {
                "node": next(n for n in nodes if n.agent_id == _AGENT),
                "src": next(s for s in sources if s.source == _UUID),
                "overview": overview,
            }
        finally:
            await _purge(pyp, dc)
            await pyp.dispose()
            await dc.dispose()

    res = asyncio.run(main())
    node = res["node"]
    assert node.online is True and node.slot_n == 4 and node.slot_used == 1
    assert node.ok == 2 and node.fail == 0 and node.success_rate == 1.0  # 两条成功都归它、无失败

    src = res["src"]
    assert src.total == 4 and src.ok == 2 and src.fail == 2
    assert src.success_rate == 0.5
    assert src.by_error == {"解析失败": 1, "超时": 1}
    # 质量：解析计数总 4（ok=3, blank=1, fail=0）→ ok_rate=0.75, blank_rate=0.25
    assert src.quality is not None
    assert src.quality.parse_ok_rate == 0.75
    assert src.quality.blank_rate == 0.25
    assert src.quality.parse_fail_rate == 0.0

    ov = res["overview"]
    assert ov.nodes_total >= 1 and ov.nodes_online >= 1
    assert ov.ok >= 2 and ov.fail >= 2
    assert ov.quality is not None


def test_monitor_endpoints_gated(require_pg: None) -> None:
    """HTTP 端点存在且经 monitor.read 闸门（关时直通 200）。"""

    async def seed_super() -> int:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await seed_default_rbac(pyp)
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username = 'mon-super')")
                )
                await conn.execute(text("DELETE FROM users WHERE username = 'mon-super'"))
                uid = (
                    await conn.execute(
                        pg_insert(User.__table__)
                        .values(username="mon-super", password_hash="x", status="active")
                        .returning(User.id)
                    )
                ).scalar_one()
            await make_superuser(pyp, "mon-super")
            return int(uid)
        finally:
            await pyp.dispose()

    async def cleanup() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username = 'mon-super')")
                )
                await conn.execute(text("DELETE FROM users WHERE username = 'mon-super'"))
        finally:
            await pyp.dispose()

    uid = asyncio.run(seed_super())
    settings = get_server_settings()
    settings.rbac_enabled = True
    try:
        with TestClient(app) as client:
            # 未登录 → 401
            assert client.get("/api/monitor/overview").status_code == 401
            # 超级用户 → 三个端点均 200 且形状正确
            client.cookies.set(COOKIE_NAME, create_session(uid, "mon-super"))
            ov = client.get("/api/monitor/overview")
            assert ov.status_code == 200 and "success_rate" in ov.json()
            assert client.get("/api/monitor/nodes").status_code == 200
            assert client.get("/api/monitor/sources").status_code == 200
    finally:
        settings.rbac_enabled = False
        asyncio.run(cleanup())
