"""apps/server 空壳冒烟：健康检查、OpenAPI 含契约 schema、契约 round-trip、agent WS 握手。"""

from __future__ import annotations

import payipa_contracts as c
from fastapi.testclient import TestClient
from pyp_server.main import app

client = TestClient(app)


def test_healthz_no_db() -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["contract_version"] == c.CONTRACT_VERSION


def test_openapi_exposes_contract_schemas() -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schemas = r.json()["components"]["schemas"]
    for name in ("TaskSpec", "NodeSnapshot", "QueueStat", "BatchProgress", "TaskAssign", "RulePointer"):
        assert name in schemas, name
    # 已生效/未生效 标注随 OpenAPI 暴露
    assert schemas["TaskSpec"]["properties"]["group"]["x-effective"] is False
    assert schemas["TaskSpec"]["properties"]["task_id"]["x-effective"] is True


def test_docs_and_redoc_served() -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").headers["content-type"].startswith("application/json")


def test_task_preview_roundtrip() -> None:
    spec = {
        "task_id": "t1",
        "req_id": "rq1",
        "batch_id": "b1",
        "source": "demo",
        "target": "https://example.com",
        "rule_ptr": {"rule_id": "r1", "version": 1, "content_hash": "deadbeef"},
    }
    r = client.post("/api/tasks/preview", json=spec)
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "task_assign"
    assert body["task"]["rule_ptr"]["content_hash"] == "deadbeef"


def test_agent_ws_register_handshake() -> None:
    # 默认 join token = "dev"；握手须带 Authorization: Bearer dev
    with client.websocket_connect("/ws/agent", headers={"authorization": "Bearer dev"}) as conn:
        conn.send_json({"type": "register", "agent_id": "a1", "hostname": "h1", "slot_n": 4})
        ack = conn.receive_json()
    assert ack["type"] == "register_ack"
    assert ack["node_token"]
    assert ack["contract_version"] == c.CONTRACT_VERSION


def test_agent_ws_rejects_bad_join_token() -> None:
    """错误/缺失 join token → 握手被拒（close 1008），不进入注册。"""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/agent", headers={"authorization": "Bearer wrong"}) as conn,
    ):
        conn.receive_json()


def test_agent_ws_register_fail_closed_in_production(monkeypatch) -> None:
    """P0-07：生产环境注册落库失败 → 拒接（1011），不得退回内存默认注册。"""
    import pytest
    from pyp_server.settings import get_server_settings
    from starlette.websockets import WebSocketDisconnect

    async def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setenv("PYP_SERVER_ENVIRONMENT", "production")
    get_server_settings.cache_clear()
    monkeypatch.setattr("pyp_server.routers.ws.auth_node", boom)  # 凭证查找失败 → 走 join token 路径
    monkeypatch.setattr("pyp_server.routers.ws.register_agent", boom)
    try:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/agent", headers={"authorization": "Bearer dev"}) as conn,
        ):
            conn.send_json({"type": "register", "agent_id": "afc", "hostname": "h1", "slot_n": 4})
            conn.receive_json()
    finally:
        get_server_settings.cache_clear()


def test_livez_and_version() -> None:
    """P0-06：/livez 零依赖存活；/version 返回版本指纹且永不 500。"""
    r = client.get("/livez")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    v = client.get("/version")
    assert v.status_code == 200
    body = v.json()
    assert body["contracts"] == c.CONTRACT_VERSION
    assert set(body) >= {"server", "contracts", "commit", "schema"}
    assert "expected_head" in body["schema"]


def test_readyz_reports_components() -> None:
    """P0-06：/readyz 返回分项结果；测试环境后台环关闭 → 报 disabled 而非失败。"""
    from pyp_server.routers import health as health_mod

    health_mod._ready_cache["resp"] = None  # 清短缓存，避免拿到上个用例的结果
    r = client.get("/readyz")
    body = r.json()
    checks = body["checks"]
    assert set(checks) >= {"config", "db.pyp", "db.data_center", "db.business", "migrations", "storage"}
    assert checks["loop.dispatch"] == "disabled" and checks["loop.consumer"] == "disabled"
    # 本机三库可达且迁移到 head 时应 ready；不可达时必须 503 且分项能定位
    if r.status_code == 200:
        assert body["status"] == "ready" and checks["migrations"] == "ok"
    else:
        assert r.status_code == 503 and any(v.startswith("error") for v in checks.values())
