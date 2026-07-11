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
    engines: frozenset[str] = field(default_factory=lambda: frozenset({"http"}))
    generation: int = 0  # 连接代次：同 id 重连递增；旧连接收尾不得误删新连接（P0-08）


class AgentHub:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConn] = {}
        self._gen = 0  # 单调连接代次计数

    def register(
        self,
        agent_id: str,
        ws: WebSocket,
        slot_n: int,
        *,
        weight: int = 1,
        group_name: str | None = None,
        engines: list[str] | None = None,
    ) -> tuple[int, AgentConn | None]:
        """登记连接；返回 (本连接代次, 被顶替的旧连接或 None)。调用方负责关闭旧连接的 WS。"""
        old = self._agents.get(agent_id)
        self._gen += 1
        self._agents[agent_id] = AgentConn(
            agent_id,
            ws,
            slot_n,
            slot_n,
            weight=weight,
            group_name=group_name,
            engines=frozenset(engines or ["http"]),
            generation=self._gen,
        )
        return self._gen, old

    def unregister(self, agent_id: str, generation: int | None = None) -> bool:
        """注销连接；带 generation 时仅当代次吻合才删（旧连接的 finally 不能删掉新连接）。

        返回是否真的删除了——调用方据此决定是否执行断连清理（回收在途/标记离线）。
        """
        conn = self._agents.get(agent_id)
        if conn is None or (generation is not None and conn.generation != generation):
            return False
        del self._agents[agent_id]
        return True

    def update_heartbeat(self, agent_id: str) -> None:
        """心跳只刷新存活标记。**槽位/在途仍以服务端 on_dispatched/on_finished 记账为准**——

        agent 现已诚实自报 free_slots/inflight（conn.py），但不据此改 hub 记账：派发在途窗口内
        （已 on_dispatched、TaskAssign 尚未被 agent 收妥登记）自报会偏大，直接采纳会超发。基于自报的
        安全对账（只补不减/检测漂移）留后续切片。"""
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.last_seen = time.monotonic()

    def pick_free(self, group: str | None = None, engine: str | None = None) -> AgentConn | None:
        """取一个可派发的在线 agent。按 (空闲槽, 权重) 择优（多空闲优先、平手权重高者优先）。

        group 非空时只在同组节点里选（分组亲和：任务 group ↔ agent group_name）；无同组空闲则返回 None，
        该任务留排队等同组节点空闲——不会错派到别组。group 为 None（未分组任务）可派给任意空闲节点。
        """
        candidates = [
            c
            for c in self._agents.values()
            if c.free_slots > 0 and (group is None or c.group_name == group) and (engine is None or engine in c.engines)
        ]
        return max(candidates, key=lambda c: (c.free_slots, c.weight)) if candidates else None

    def free_caps(self) -> dict[str | None, set[str]]:
        """空闲节点能力汇总：{None: 全部空闲节点引擎并集, 组名: 该组空闲节点引擎并集}。

        None 键对应未分组请求（可派任意空闲节点）；分组请求只匹配同组键。空 dict = 无空闲节点。
        供派发环把能力过滤下推到 SQL（P0-11 防队头饥饿）。
        """
        caps: dict[str | None, set[str]] = {}
        for c in self._agents.values():
            if c.free_slots <= 0:
                continue
            caps.setdefault(None, set()).update(c.engines)
            if c.group_name is not None:
                caps.setdefault(c.group_name, set()).update(c.engines)
        return caps

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
