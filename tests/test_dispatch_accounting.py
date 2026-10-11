"""派发发送与 WS 结果回报交错时，槽位必须仍归属于原连接和执行尝试。"""

from __future__ import annotations

import anyio
import pytest
from payipa_contracts import RulePointer, TaskSpec
from pyp_server import scheduler
from pyp_server.hub import AgentHub
from pyp_server.ratelimit import SourceRateLimiter


@pytest.mark.parametrize("outcome", ["fast_result", "failed_send", "reconnect", "new_attempt"])
def test_send_reservation_and_failure_rollback_are_fenced(monkeypatch, outcome):
    hub = AgentHub()
    requeued = []
    generation = 0

    class Socket:
        async def send_text(self, message):
            assert hub.pick_free() is None, "slot must be reserved before awaiting send"
            if outcome == "fast_result":
                hub.on_finished("a", "1", attempt=1, generation=generation)
                return
            if outcome == "reconnect":
                current, _ = hub.register("a", object(), 1)
                hub.on_dispatched("a", "1", attempt=2, generation=current)
            elif outcome == "new_attempt":
                hub.on_finished("a", "1", attempt=1, generation=generation)
                hub.on_dispatched("a", "1", attempt=2, generation=generation)
            raise OSError("send failed")

    generation, _ = hub.register("a", Socket(), 1)
    task = TaskSpec(
        task_id="1",
        batch_id="1",
        req_id="1",
        source="demo",
        target="https://example.invalid",
        rule_ptr=RulePointer(rule_id="1", version=1, content_hash="test"),
        attempt=1,
    )
    windows = iter([[task], []])

    async def claim(*args, **kwargs):
        return next(windows)

    async def assigned(*args, **kwargs):
        return 1

    async def rates(*args):
        return {}

    async def requeue(engine, req_id, *, agent_id, attempt):
        assert agent_id == "a" and attempt == 1
        requeued.append(req_id)

    monkeypatch.setattr(scheduler, "claim_queued_for_dispatch", claim)
    monkeypatch.setattr(scheduler, "mark_assigned", assigned)
    monkeypatch.setattr(scheduler, "source_rate_limits", rates)
    monkeypatch.setattr(scheduler, "requeue_request", requeue)
    monkeypatch.setattr(scheduler, "issue_rule_token", lambda *args, **kwargs: "test")

    count = anyio.run(lambda: scheduler.drain_once(hub, object(), "test", 60, SourceRateLimiter()))
    if outcome == "fast_result":
        assert count == 1 and requeued == []
        assert hub.get("a").free_slots == 1
    elif outcome == "failed_send":
        assert count == 0 and requeued == [1]
        assert hub.get("a").free_slots == 1
    else:
        assert count == 0 and requeued == [1]
        assert hub.get("a").free_slots == 0
        assert hub.get("a").inflight == {"1": 2}


def test_connection_replaced_during_assignment_is_not_sent_old_work(monkeypatch):
    hub = AgentHub()
    generation, _ = hub.register("a", object(), 1)
    requeued = []
    task = TaskSpec(
        task_id="1",
        batch_id="1",
        req_id="1",
        source="demo",
        target="https://example.invalid",
        rule_ptr=RulePointer(rule_id="1", version=1, content_hash="test"),
    )

    async def claim(*args, **kwargs):
        return [task]

    async def assigned(*args, **kwargs):
        hub.register("a", object(), 1)
        return 1

    async def rates(*args):
        return {}

    async def requeue(engine, req_id, *, agent_id, attempt):
        assert agent_id == "a" and attempt == 0
        requeued.append(req_id)

    monkeypatch.setattr(scheduler, "claim_queued_for_dispatch", claim)
    monkeypatch.setattr(scheduler, "mark_assigned", assigned)
    monkeypatch.setattr(scheduler, "source_rate_limits", rates)
    monkeypatch.setattr(scheduler, "requeue_request", requeue)
    monkeypatch.setattr(scheduler, "issue_rule_token", lambda *args, **kwargs: "test")
    assert anyio.run(lambda: scheduler.drain_once(hub, object(), "test", 60, SourceRateLimiter())) == 0
    assert requeued == [1]
    assert hub.get("a").generation != generation
    assert hub.get("a").free_slots == 1


def test_retry_can_use_another_agent_while_old_attempt_still_occupies_a_slot(monkeypatch):
    hub = AgentHub()
    sent = []

    class Socket:
        async def send_text(self, message):
            sent.append("new-agent")

    hub.register("old-agent", object(), 4)
    hub.on_dispatched("old-agent", "1", attempt=0)
    hub.register("new-agent", Socket(), 2)
    task = TaskSpec(
        task_id="1",
        batch_id="1",
        req_id="1",
        source="demo",
        target="https://example.invalid",
        rule_ptr=RulePointer(rule_id="1", version=1, content_hash="test"),
        attempt=1,
    )
    windows = iter([[task], []])

    async def claim(*args, **kwargs):
        return next(windows)

    async def assigned(engine, req_id, agent_id, **kwargs):
        assert agent_id == "new-agent"
        return 1

    async def rates(*args):
        return {}

    monkeypatch.setattr(scheduler, "claim_queued_for_dispatch", claim)
    monkeypatch.setattr(scheduler, "mark_assigned", assigned)
    monkeypatch.setattr(scheduler, "source_rate_limits", rates)
    monkeypatch.setattr(scheduler, "issue_rule_token", lambda *args, **kwargs: "test")
    assert anyio.run(lambda: scheduler.drain_once(hub, object(), "test", 60, SourceRateLimiter())) == 1
    assert sent == ["new-agent"]
    assert hub.get("old-agent").inflight == {"1": 0}
    assert hub.get("new-agent").inflight == {"1": 1}
