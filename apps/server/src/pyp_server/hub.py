"""在线 agent 连接注册表 + 下发（server 运行态；核心业务在 payipa-core）。

M1：内存态，进程内单例（app.state.hub）。槽位信用制：只向有空闲槽的 agent 下发（07 定案）。
崩溃丢连接可接受——权威状态在 PG，重连后重建（M2 完善租约/回收）。
"""

from __future__ import annotations

import time
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
    last_seen: float = 0.0  # 单调时钟：最近一次心跳（供后续 liveness reaper；本切片不做超时判定）
    weight: int = 1  # 加权派发：来自 agents 表（管理员预置），平手时权重高者优先
    group_name: str | None = None  # 能力分组：任务带 group 时只派给同组节点（分组亲和）


class AgentHub:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConn] = {}

    def register(
        self, agent_id: str, ws: WebSocket, slot_n: int, *, weight: int = 1, group_name: str | None = None
    ) -> None:
        self._agents[agent_id] = AgentConn(agent_id, ws, slot_n, slot_n, weight=weight, group_name=group_name)

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def update_heartbeat(self, agent_id: str) -> None:
        """心跳只刷新存活标记。**槽位/在途以服务端 on_dispatched/on_finished 记账为准**——
        agent 自报 free_slots/inflight 当前不可信（conn.py 占位恒报 slot_n/空），不能用来驱动派发，
        否则心跳会周期性抹掉派发记账、导致超发。真实自报的对账留后续 M2 切片。"""
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.last_seen = time.monotonic()

    def pick_free(self, group: str | None = None) -> AgentConn | None:
        """取一个可派发的在线 agent。按 (空闲槽, 权重) 择优（多空闲优先、平手权重高者优先）。

        group 非空时只在同组节点里选（分组亲和：任务 group ↔ agent group_name）；无同组空闲则返回 None，
        该任务留排队等同组节点空闲——不会错派到别组。group 为 None（未分组任务）可派给任意空闲节点。
        """
        candidates = [
            c
            for c in self._agents.values()
            if c.free_slots > 0 and (group is None or c.group_name == group)
        ]
        return max(candidates, key=lambda c: (c.free_slots, c.weight)) if candidates else None

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
