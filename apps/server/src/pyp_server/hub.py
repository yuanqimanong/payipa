"""在线 agent 连接注册表 + 下发（server 运行态；核心业务在 payipa-core）。

M1：内存态，进程内单例（app.state.hub）。槽位信用制：只向有空闲槽的 agent 下发（07 定案）。
崩溃丢连接可接受——权威状态在 PG，重连后重建（M2 完善租约/回收）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import WebSocket
from payipa_contracts import NodeSnapshot, ServerFrame


@dataclass
class AgentConn:
    agent_id: str
    ws: WebSocket
    slot_n: int
    free_slots: int
    inflight: set[str] = field(default_factory=set)


class AgentHub:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConn] = {}

    def register(self, agent_id: str, ws: WebSocket, slot_n: int) -> None:
        self._agents[agent_id] = AgentConn(agent_id, ws, slot_n, slot_n)

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def update_heartbeat(self, agent_id: str, free_slots: int, inflight: list[str]) -> None:
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.free_slots = free_slots
            conn.inflight = set(inflight)

    def pick_free(self) -> AgentConn | None:
        """取空闲槽最多的在线 agent（平手取任意）。"""
        candidates = [c for c in self._agents.values() if c.free_slots > 0]
        return max(candidates, key=lambda c: c.free_slots) if candidates else None

    def on_dispatched(self, agent_id: str, req_id: str) -> None:
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.free_slots = max(0, conn.free_slots - 1)
            conn.inflight.add(req_id)

    def on_finished(self, agent_id: str, req_id: str) -> None:
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.free_slots = min(conn.slot_n, conn.free_slots + 1)
            conn.inflight.discard(req_id)

    def find_by_req(self, req_id: str) -> AgentConn | None:
        return next((c for c in self._agents.values() if req_id in c.inflight), None)

    def snapshots(self) -> list[NodeSnapshot]:
        return [
            NodeSnapshot(
                agent_id=c.agent_id,
                online=True,
                slot_n=c.slot_n,
                slot_used=c.slot_n - c.free_slots,
                inflight=sorted(c.inflight),
            )
            for c in self._agents.values()
        ]

    async def send_frame(self, agent_id: str, frame: ServerFrame) -> None:
        conn = self._agents.get(agent_id)
        if conn is not None:
            await conn.ws.send_text(frame.model_dump_json())
