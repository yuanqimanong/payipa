"""资源关停顺序与取消屏蔽：后台任务停止后才能释放锁和连接池。"""

from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest
from pyp_server import lifecycle
from pyp_server.settings import ServerSettings


class _Engine:
    def __init__(self, name, events, *, fail=False):
        self.name, self.events, self.fail = name, events, fail

    async def dispose(self):
        await anyio.sleep(0)
        self.events.append(f"dispose:{self.name}")
        if self.fail:
            raise OSError(f"dispose failed: {self.name}")


class _Engines:
    def __init__(self, events, *, fail=None):
        self.events = events
        self.engines = {key: _Engine(key, events, fail=key == fail) for key in ("pyp", "data_center", "business")}

    def __call__(self, key):
        return self.engines[key]

    def cache_clear(self):
        self.events.append("clear:engines")


class _Connection:
    def __init__(self, events):
        self.events = events

    async def invalidate(self):
        await anyio.sleep(0)
        self.events.append("invalidate:lock")

    async def aclose(self):
        await anyio.sleep(0)
        self.events.append("close:lock")


def _install_resources(monkeypatch, events, *, fail=None):
    monkeypatch.setattr(lifecycle, "get_engine", _Engines(events, fail=fail))
    monkeypatch.setattr(
        lifecycle, "get_sessionmaker", SimpleNamespace(cache_clear=lambda: events.append("clear:sessions"))
    )


def test_cancelled_lifespan_waits_for_worker_then_releases_lock(monkeypatch) -> None:
    events = []
    _install_resources(monkeypatch, events)

    async def unlock(conn):
        await anyio.sleep(0)
        events.append("unlock:lock")

    monkeypatch.setattr(lifecycle, "unlock", unlock)

    async def run():
        started = anyio.Event()

        async def worker(app):
            started.set()
            try:
                await anyio.sleep_forever()
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.sleep(0)
                    events.append("worker:stopped")

        monkeypatch.setattr(lifecycle, "dispatch_loop", worker)
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=ServerSettings(
                    dispatch_enabled=True,
                    push_enabled=False,
                    single_worker_guard=False,
                )
            )
        )
        with anyio.CancelScope() as scope:
            async with lifecycle.lifespan(app):
                await started.wait()
                app.state.lock_conn = _Connection(events)
                scope.cancel()
        assert app.state.lock_conn is None

    anyio.run(run)
    assert events == [
        "worker:stopped",
        "unlock:lock",
        "close:lock",
        "dispose:pyp",
        "dispose:data_center",
        "dispose:business",
        "clear:sessions",
        "clear:engines",
    ]


def test_cleanup_failure_still_disconnects_lock_and_disposes_other_pools(monkeypatch) -> None:
    events = []
    _install_resources(monkeypatch, events, fail="pyp")

    async def unlock(conn):
        events.append("unlock:failed")
        raise OSError("lock connection lost")

    monkeypatch.setattr(lifecycle, "unlock", unlock)
    app = SimpleNamespace(state=SimpleNamespace(lock_conn=_Connection(events)))
    with pytest.raises(ExceptionGroup) as raised:
        anyio.run(lambda: lifecycle._release_resources(app))
    assert len(raised.value.exceptions) == 2
    assert events == [
        "unlock:failed",
        "invalidate:lock",
        "close:lock",
        "dispose:pyp",
        "dispose:data_center",
        "dispose:business",
        "clear:sessions",
        "clear:engines",
    ]
    assert app.state.lock_conn is None


def test_cancelled_lock_acquisition_disconnects_untracked_session(monkeypatch) -> None:
    events = []

    async def run():
        entered = anyio.Event()
        connection = _Connection(events)

        class Engine:
            async def connect(self):
                return connection

        async def try_lock(conn):
            entered.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(lifecycle, "get_engine", lambda key: Engine())
        monkeypatch.setattr(lifecycle, "try_lock", try_lock)
        app = SimpleNamespace(state=SimpleNamespace(lock_conn=None))
        async with anyio.create_task_group() as group:
            group.start_soon(lifecycle._guarded_loops, app, group)
            await entered.wait()
            group.cancel_scope.cancel()

    anyio.run(run)
    assert events == ["invalidate:lock", "close:lock"]
