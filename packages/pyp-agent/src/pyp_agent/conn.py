"""出站 WebSocket 连接（注册→心跳→领任务→回报→取消）。

M0 骨架：定义接口与帧类型引用；实现于 M1/M2（websockets + anyio task group / cancel scope，
断线指数退避重连）。只用 payipa-contracts 的帧 schema，零 DB/零 S3 密钥。
"""

from __future__ import annotations

from payipa_contracts import (
    Capabilities,
    Heartbeat,
    RegisterReq,
    ServerFrame,
    StatusReport,
)


class AgentConnection:
    """一条常驻出站 WS，多路复用注册/心跳/任务/状态/取消。"""

    def __init__(self, server: str, token: str, slot_n: int | None = None) -> None:
        self.server = server
        self.token = token
        self.slot_n = slot_n or 0

    def _register_frame(self, agent_id: str, hostname: str) -> RegisterReq:
        return RegisterReq(
            agent_id=agent_id,
            hostname=hostname,
            capabilities=Capabilities(),
            slot_n=self.slot_n,
        )

    async def run(self) -> None:
        """连接主循环：注册→心跳→领任务→回报；断线指数退避重连。"""
        raise NotImplementedError("M1/M2：websockets 出站 + anyio 结构化并发 + 退避重连")

    async def _handle_server_frame(self, frame: ServerFrame) -> None:
        """分发主控下发帧（task_assign / cancel / register_ack）。"""
        raise NotImplementedError("M1/M2：按 frame.type 分发到抓取/取消")

    async def _heartbeat(self) -> Heartbeat:
        raise NotImplementedError("M2：上报在途清单与空闲槽（槽位信用制）")

    async def _report(self, req_id: str, state: int) -> StatusReport:
        raise NotImplementedError("M1：回报请求任务状态与结果指针")
