"""Readiness 必须在故障时有界、合并同应用的并发刷新，并等待首个成功心跳。"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import anyio
import pytest
from pyp_server.main import create_app
from pyp_server.routers import health
from pyp_server.runtime import LoopHealth
from pyp_server.settings import ServerSettings


def _request(monkeypatch):
    app = create_app()
    app.state.settings = ServerSettings(dispatch_enabled=False, push_enabled=False)
    monkeypatch.setattr(health, "_BLOCKING_PROBE_LOCKS", {name: threading.Lock() for name in ("storage", "migrations")})

    async def ok(*args):
        return "ok"

    monkeypatch.setattr(health, "_ping_db", ok)
    monkeypatch.setattr(health, "_check_migrations", ok)
    monkeypatch.setattr(health, "_check_dynamic_schemas", ok)
    monkeypatch.setattr(health, "_check_storage", lambda: "ok")
    return SimpleNamespace(app=app)


def test_concurrent_readiness_requests_share_one_refresh(monkeypatch):
    request = _request(monkeypatch)
    calls = []
    responses = []

    async def ping(key):
        calls.append(key)
        await anyio.sleep(0.01)
        return "ok"

    monkeypatch.setattr(health, "_ping_db", ping)

    async def probe():
        responses.append(await health.readyz(request))

    async def run():
        async with anyio.create_task_group() as group:
            for _ in range(10):
                group.start_soon(probe)

    anyio.run(run)
    assert sorted(calls) == ["business", "data_center", "pyp"]
    assert len(responses) == 10 and all(r.status_code == 200 for r in responses)


def test_probe_checks_run_concurrently(monkeypatch):
    request = _request(monkeypatch)

    async def run():
        entered = set()
        all_entered = anyio.Event()

        async def probe(name):
            entered.add(name)
            if len(entered) == 5:
                all_entered.set()
            await all_entered.wait()
            return "ok"

        monkeypatch.setattr(health, "_ping_db", probe)
        monkeypatch.setattr(health, "_check_migrations", lambda: probe("migrations"))
        monkeypatch.setattr(health, "_check_dynamic_schemas", lambda: probe("schemas"))
        response = None
        with anyio.move_on_after(0.5):
            response = await health.readyz(request)
        assert response is not None, "serial checks cannot make progress through the barrier"
        assert response.status_code == 200

    anyio.run(run)


@pytest.mark.parametrize("stuck_check", ["_ping_db", "_check_migrations", "_check_dynamic_schemas"])
def test_stuck_probe_returns_unavailable_and_keeps_other_results(monkeypatch, stuck_check):
    request = _request(monkeypatch)
    monkeypatch.setattr(health, "_CHECK_TIMEOUT_S", 0.02, raising=False)

    async def stuck(*args):
        await anyio.sleep_forever()

    monkeypatch.setattr(health, stuck_check, stuck)

    async def run():
        response = None
        with anyio.move_on_after(0.5):
            response = await health.readyz(request)
        assert response is not None, "readiness must time out its own probes"
        assert response.status_code == 503
        checks = json.loads(response.body)["checks"]
        assert checks["storage"] == "ok"
        key = {"_ping_db": "db.pyp", "_check_migrations": "migrations", "_check_dynamic_schemas": "dynamic_schemas"}
        assert checks[key[stuck_check]] == "error: TimeoutError"

    anyio.run(run)


def test_refresh_cancellation_does_not_cache_partial_result_or_hold_lock(monkeypatch):
    request = _request(monkeypatch)

    async def run():
        entered = anyio.Event()

        async def stuck(*args):
            entered.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(health, "_check_migrations", stuck)
        async with anyio.create_task_group() as group:
            group.start_soon(health.readyz, request)
            await entered.wait()
            group.cancel_scope.cancel()
        assert request.app.state.readiness_cache["resp"] is None

        async def ok():
            return "ok"

        monkeypatch.setattr(health, "_check_migrations", ok)
        with anyio.fail_after(0.5):
            assert (await health.readyz(request)).status_code == 200

    anyio.run(run)


def test_probe_cancellation_propagates_without_caching_incomplete_success(monkeypatch):
    request = _request(monkeypatch)
    cancellation = asyncio.CancelledError("probe canceled")

    async def canceled():
        raise cancellation

    monkeypatch.setattr(health, "_check_migrations", canceled)
    with pytest.raises(asyncio.CancelledError) as raised:
        anyio.run(lambda: health.readyz(request))
    assert raised.value is cancellation
    assert request.app.state.readiness_cache["resp"] is None


def test_blocked_storage_probe_does_not_block_event_loop_or_response(monkeypatch):
    request = _request(monkeypatch)
    monkeypatch.setattr(health, "_CHECK_TIMEOUT_S", 0.05)
    release = threading.Event()
    entered = threading.Event()

    def blocked_disk():
        entered.set()
        release.wait(timeout=1)
        return "ok"

    monkeypatch.setattr(health, "_check_storage", blocked_disk)
    try:
        response = anyio.run(lambda: health.readyz(request))
        assert entered.is_set()
        assert response.status_code == 503
        assert json.loads(response.body)["checks"]["storage"] == "error: TimeoutError"
    finally:
        release.set()


def test_probe_error_is_reported_without_canceling_other_checks(monkeypatch):
    request = _request(monkeypatch)

    async def failed():
        raise OSError("migration metadata unavailable")

    monkeypatch.setattr(health, "_check_migrations", failed)
    response = anyio.run(lambda: health.readyz(request))
    assert response.status_code == 503
    checks = json.loads(response.body)["checks"]
    assert checks["migrations"] == "error: OSError"
    assert checks["dynamic_schemas"] == checks["storage"] == checks["db.pyp"] == "ok"


@pytest.mark.parametrize("name", ["storage", "migrations"])
def test_repeated_timeout_does_not_accumulate_blocked_workers_and_recovers(monkeypatch, name):
    original_migrations = health._check_migrations
    request = _request(monkeypatch)
    monkeypatch.setattr(health, "_CACHE_TTL_S", 0)
    monkeypatch.setattr(health, "_CHECK_TIMEOUT_S", 0.05)
    release = threading.Event()
    calls = []

    def blocked():
        calls.append("called")
        release.wait(timeout=2)
        return "ok"

    if name == "storage":
        monkeypatch.setattr(health, "_check_storage", blocked)
    else:
        monkeypatch.setattr(health, "_check_migrations", original_migrations)
        monkeypatch.setattr(health, "script_head", blocked)
        monkeypatch.setattr(health, "get_engine", lambda key: object())

        async def revision(engine):
            return "ok"

        monkeypatch.setattr(health, "db_revision", revision)

    async def run():
        try:
            for index in range(5):
                response = await health.readyz(request)
                assert response.status_code == 503
                error = json.loads(response.body)["checks"][name]
                assert error == ("error: TimeoutError" if index == 0 else "error: ProbeInProgressError")
            assert calls == ["called"]
        finally:
            release.set()
        # 等真实工作线程释放锁，之后探针可以重新成功。
        with anyio.fail_after(1):
            while health._BLOCKING_PROBE_LOCKS[name].locked():
                await anyio.sleep(0)
        assert (await health.readyz(request)).status_code == 200
        assert calls == ["called", "called"]

    anyio.run(run)


def test_cache_ttl_begins_when_slow_refresh_completes(monkeypatch):
    request = _request(monkeypatch)
    now = [100.0]
    calls = []
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def migrations():
        calls.append("migrations")
        now[0] += health._CACHE_TTL_S + 1
        return "ok"

    monkeypatch.setattr(health, "_check_migrations", migrations)

    async def run():
        first = await health.readyz(request)
        assert request.app.state.readiness_cache["at"] == now[0]
        now[0] += health._CACHE_TTL_S - 0.1
        assert await health.readyz(request) is first
        assert calls == ["migrations"]

    anyio.run(run)


def test_readiness_uses_the_running_app_settings(monkeypatch):
    request = _request(monkeypatch)
    monkeypatch.setattr(health, "get_server_settings", lambda: ServerSettings(dispatch_enabled=True, push_enabled=True))
    assert anyio.run(lambda: health.readyz(request)).status_code == 200


def test_loop_is_not_ready_before_its_first_successful_tick():
    loop = LoopHealth("dispatch", 1)
    app = SimpleNamespace(state=SimpleNamespace(loop_health={"dispatch": loop}))
    assert not loop.fresh()
    assert health._check_loop(app, "dispatch", True).startswith("error:")
    loop.ok()
    assert loop.fresh()
    assert health._check_loop(app, "dispatch", True) == "ok"
