"""单测（无需 PG）：后台派发环的 GC 调度 + 阶段异常隔离。

- D1：`gc_expired_artifacts` 必须被后台环真正调度（此前已实现却从未被调用，会磁盘写满型宕机）。
- D3：某个阶段抛异常时，同轮其它阶段仍执行、循环不退出（此前整轮包在单 try 里，一条毒丸拖垮全部派发）。

所有阶段函数被替换为桩，循环不触碰真实数据库，仅验证调度/隔离的编排语义。
"""

from __future__ import annotations

import types

import anyio
import pyp_server.scheduler as sch
from pyp_server.settings import ServerSettings


def test_dispatch_loop_schedules_gc_and_isolates_stage_failure(monkeypatch) -> None:
    calls = {"gc": 0, "reconcile": 0, "fire": 0, "requeue": 0, "sweep": 0, "drain": 0}

    async def _gc(dc, storage, **k):
        calls["gc"] += 1
        return calls["gc"]

    async def _reconcile(pyp, dc):
        calls["reconcile"] += 1
        return {"checked": 0}

    async def _fire(pyp, now):  # 毒丸阶段：每轮都抛
        calls["fire"] += 1
        raise RuntimeError("boom")

    async def _requeue(pyp, **k):
        calls["requeue"] += 1
        return 0

    async def _sweep(pyp):
        calls["sweep"] += 1
        return 0

    async def _drain(*a):
        calls["drain"] += 1
        return 0

    monkeypatch.setattr(sch, "gc_expired_artifacts", _gc)
    monkeypatch.setattr(sch, "reconcile_data_schemas", _reconcile)
    monkeypatch.setattr(sch, "fire_due_schedules", _fire)
    monkeypatch.setattr(sch, "requeue_expired_leases", _requeue)
    monkeypatch.setattr(sch, "sweep_canceling_batches", _sweep)
    monkeypatch.setattr(sch, "drain_once", _drain)
    monkeypatch.setattr(sch, "get_engine", lambda key: object())
    monkeypatch.setattr(sch, "get_storage", lambda: object())
    monkeypatch.setattr(
        sch, "get_server_settings", lambda: ServerSettings(dispatch_interval_s=0.02, gc_interval_s=0.02)
    )

    app = types.SimpleNamespace(state=types.SimpleNamespace(hub=None, limiter=None, loop_health={}))

    async def run() -> None:
        with anyio.move_on_after(0.4):  # 跑几轮后取消
            await sch.dispatch_loop(app)

    anyio.run(run)

    # D1：GC 被调度执行
    assert calls["gc"] >= 1, calls
    # D3：fire 每轮抛错，但循环没退出（fire 被反复调用），且抛错阶段之后的阶段照常执行
    assert calls["fire"] >= 2, calls
    assert calls["drain"] >= 2, calls
    assert calls["sweep"] >= 2, calls
