"""直接运行 WS handler，以真实契约帧验证重放/迟到/旧连接结果的槽位隔离。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import pytest
from fastapi import WebSocketDisconnect
from payipa_contracts import ErrorCode
from pyp_server.hub import AgentHub
from pyp_server.routers import ws


@pytest.mark.parametrize("kind", ["result", "status", "error"])
@pytest.mark.parametrize("scenario", ["replay", "old_attempt", "old_connection"])
def test_terminal_frames_cannot_release_unrelated_slots(monkeypatch, kind, scenario):
    hub = AgentHub()
    checked = []
    monkeypatch.setattr(ws, "_reg_locks", {})
    monkeypatch.setattr(ws, "get_engine", lambda key: object())

    async def auth(*args, **kwargs):
        return "accounting-agent"

    async def register(*args, **kwargs):
        return 1, None

    async def noop(*args, **kwargs):
        return False

    monkeypatch.setattr(ws, "auth_node", auth)
    monkeypatch.setattr(ws, "register_agent", register)
    for name in (
        "_ingest_result",
        "set_request_state",
        "_finalize_terminal_request",
        "requeue_agent_inflight",
        "set_agent_offline",
    ):
        monkeypatch.setattr(ws, name, noop)

    frame = {
        "result": {
            "type": "result",
            "result": {"batch_id": "1", "req_id": "1", "attempt": 0, "summary": {"elapsed_s": 0}},
        },
        "status": {"type": "status", "req_id": "1", "attempt": 0, "state": 4},
        "error": {"type": "error", "req_id": "1", "attempt": 0, "code": int(ErrorCode.SOFT_FAIL), "message": "test"},
    }[kind]
    frames = iter(
        [
            {"type": "register", "agent_id": "accounting-agent", "hostname": "test", "slot_n": 2},
            *([frame, frame] if scenario == "replay" else [frame]),
        ]
    )

    class Socket:
        headers = {"authorization": "Bearer test"}
        app = SimpleNamespace(state=SimpleNamespace(hub=hub, limiter=None))

        async def accept(self):
            pass

        async def send_text(self, text):
            if json.loads(text)["type"] != "register_ack":
                return
            if scenario == "old_connection":
                hub.register("accounting-agent", object(), 2)
            generation = hub.get("accounting-agent").generation
            hub.on_dispatched("accounting-agent", "1", attempt=int(scenario == "old_attempt"), generation=generation)
            hub.on_dispatched("accounting-agent", "2", generation=generation)

        async def receive_json(self):
            try:
                return next(frames)
            except StopIteration:
                current = hub.get("accounting-agent")
                assert current.free_slots == (1 if scenario == "replay" else 0)
                assert sorted(current.inflight) == (["2"] if scenario == "replay" else ["1", "2"])
                checked.append(True)
                raise WebSocketDisconnect() from None

    anyio.run(lambda: ws.agent_ws(Socket()))
    assert checked == [True]
