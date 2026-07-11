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
