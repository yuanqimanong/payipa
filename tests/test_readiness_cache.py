"""readiness 结果只缓存于当前应用，不能污染另一个实例或重启后的判断。"""

from __future__ import annotations

import anyio
from fastapi.testclient import TestClient
from pyp_server.main import create_app
from pyp_server.routers import health


def test_readiness_cache_is_per_application(monkeypatch) -> None:
    calls = []

    async def ping(key):
        calls.append(key)
        return "ok"

    async def ok():
        return "ok"

    monkeypatch.setattr(health, "_ping_db", ping)
    monkeypatch.setattr(health, "_check_migrations", ok)
    monkeypatch.setattr(health, "_check_dynamic_schemas", ok)
    monkeypatch.setattr(health, "_check_storage", lambda: "ok")
    with TestClient(create_app()) as first:
        assert first.get("/readyz").status_code == 200
        assert first.get("/readyz").status_code == 200
    assert len(calls) == 3  # 同一 app 的短缓存复用

    async def unavailable(key):
        calls.append(key)
        return "error: database down"

    monkeypatch.setattr(health, "_ping_db", unavailable)
    with TestClient(create_app()) as second:
        response = second.get("/readyz")
        assert response.status_code == 503
        assert response.json()["checks"]["db.pyp"] == "error: database down"
    assert len(calls) == 6  # 新 app 不得命中第一实例的 200


def test_app_restart_discards_old_probe_results() -> None:
    from pyp_server.lifecycle import lifespan

    app = create_app()

    async def run():
        async with lifespan(app):
            app.state.readiness_cache["resp"] = "stale"
        async with lifespan(app):
            assert app.state.readiness_cache["resp"] is None

    anyio.run(run)
