"""后台执行语义：阶段隔离、失败重试、低频调度、恢复与取消传播。"""

from __future__ import annotations

import asyncio
import logging

import anyio
import pytest
from pydantic import ValidationError
from pyp_server.background import PeriodicStage, run_periodic, run_stages
from pyp_server.runtime import LoopHealth
from pyp_server.settings import ServerSettings

_LOGGER = logging.getLogger(__name__)


def test_failed_stage_does_not_skip_later_stages() -> None:
    calls = []

    async def failed():
        calls.append("failed")
        raise RuntimeError("unavailable")

    async def working():
        calls.append("working")

    async def run():
        assert not await run_stages([PeriodicStage("first", failed), PeriodicStage("second", working)], _LOGGER)

    anyio.run(run)
    assert calls == ["failed", "working"]


def test_only_successful_stage_advances_its_next_run() -> None:
    calls = []

    async def action():
        calls.append("called")
        if len(calls) == 1:
            raise OSError("transient")

    async def run():
        stage = PeriodicStage("maintenance", action, interval_s=60)
        assert not await stage.run(100, _LOGGER)
        assert await stage.run(101, _LOGGER)  # 失败后下轮即可重试
        assert await stage.run(102, _LOGGER)  # 成功后到期前跳过
        assert len(calls) == 2
        assert await stage.run(161, _LOGGER)

    anyio.run(run)
    assert len(calls) == 3


def test_periodic_backoff_resets_after_success_and_preserves_cancellation() -> None:
    results = iter([False, False, True])
    delays = []
    failures = []
    health = LoopHealth("test", 1)

    async def tick():
        try:
            return next(results)
        except StopIteration:
            raise asyncio.CancelledError() from None

    async def sleep(delay):
        delays.append(delay)
        failures.append(health.consecutive_fails)

    async def run():
        await run_periodic(tick, interval_s=1.0, logger=_LOGGER, health=health, sleep=sleep)

    with pytest.raises(asyncio.CancelledError):
        anyio.run(run)
    assert delays == [1.0, 2.0, 1.0]
    assert failures == [1, 2, 0]
    assert health.last_ok_at is not None and health.last_error is None


def test_tick_exception_is_recorded_and_retried() -> None:
    calls = []
    health = LoopHealth("test", 1)

    async def tick():
        calls.append("called")
        if len(calls) == 1:
            raise OSError("database down")
        raise asyncio.CancelledError()

    async def sleep(delay):
        assert delay == 1.0
        assert health.consecutive_fails == 1
        assert health.last_error == "OSError: database down"

    with pytest.raises(asyncio.CancelledError):
        anyio.run(lambda: run_periodic(tick, interval_s=1.0, logger=_LOGGER, health=health, sleep=sleep))
    assert calls == ["called", "called"]


@pytest.mark.parametrize("interval", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_loop_interval_cannot_create_a_busy_loop(interval: float) -> None:
    async def tick():
        pytest.fail("invalid interval must be rejected before tick")

    with pytest.raises(ValueError):
        anyio.run(lambda: run_periodic(tick, interval_s=interval, logger=_LOGGER))


@pytest.mark.parametrize("interval", [-1.0, float("nan"), float("inf")])
def test_invalid_stage_interval_is_rejected(interval: float) -> None:
    async def action():
        pass

    with pytest.raises(ValueError):
        PeriodicStage("invalid", action, interval_s=interval)


@pytest.mark.parametrize("field", ["dispatch_interval_s", "push_interval_s"])
@pytest.mark.parametrize("interval", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_config_is_rejected_before_starting_workers(field: str, interval: float) -> None:
    with pytest.raises(ValidationError):
        ServerSettings(**{field: interval})
