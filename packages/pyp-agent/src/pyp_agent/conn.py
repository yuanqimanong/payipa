"""出站 WebSocket 连接（register→心跳→领任务→回报）+ 任务处理。

M1：websockets 出站 + anyio task group；处理 task_assign → 拉规则 → fetch → 解析 → 上传 raw → 回 ResultReport。
断线指数退避重连（简版）；cancel/整树取消细化留 M2。只用 payipa-contracts 帧，零 DB/零 S3 密钥。
"""

from __future__ import annotations

import time

import anyio
import websockets
from jianbing_utils.retry import backoff_delay
from payipa_contracts import (
    Cancel,
    Capabilities,
    ErrorCode,
    ErrorFrame,
    ExecSummary,
    Heartbeat,
    RegisterReq,
    RequestState,
    ResultBatch,
    ResultReport,
    ServerFrame,
    StatusReport,
    TaskAssign,
    TaskSpec,
)
from pydantic import TypeAdapter

from pyp_agent.fetch import fetch
from pyp_agent.interpret import interpret_page
from pyp_agent.rules import RuleCache
from pyp_agent.upload import upload_raw_via_server

_server_frame = TypeAdapter(ServerFrame)


def _ws_url(server_base: str) -> str:
    base = server_base.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/ws/agent"


async def process_task(
    task: TaskSpec,
    *,
    upload_token: str | None,
    server_base: str,
    rule_cache: RuleCache,
    agent_id: str,
) -> ResultReport:
    """执行一个请求任务 → ResultReport（拉规则→fetch→解析→上传 raw→回传）。"""
    started = time.monotonic()
    rule = await rule_cache.get(task.rule_ptr)
    fetched = await fetch(task.target, engine_hint=task.engine_hint, timeout=task.timeout_s)

    artifacts = []
    if upload_token:  # local 兜底：raw 经主控回传（S3 直传走 M5）
        ref = await upload_raw_via_server(
            server_base,
            upload_token,
            source_uuid=task.source,
            batch_id=task.batch_id,
            url=task.target,
            data=fetched.body,
            content_type=fetched.content_type,
            task_id=task.task_id,
            agent_id=agent_id,
        )
        artifacts.append(ref)

    parsed = interpret_page(rule, fetched.body, task.target, fetched.content_type)
    summary = ExecSummary(
        elapsed_s=round(time.monotonic() - started, 3),
        count_ok=len(parsed.items),
        count_fail=0,
        count_blank=0,
    )
    return ResultReport(
        result=ResultBatch(
            batch_id=task.batch_id,
            req_id=task.req_id,
            items=parsed.items,
            artifacts=artifacts,
            discovered=parsed.links,  # type=link/store+link 字段值 → 主控去重后并入同批入队（多波爬行）
            summary=summary,
        )
    )


class AgentConnection:
    """一条常驻出站 WS，多路复用注册/心跳/任务/状态。"""

    def __init__(
        self,
        server: str,
        token: str,
        *,
        slot_n: int = 4,
        agent_id: str = "agent-1",
        hostname: str = "localhost",
        heartbeat_s: float = 20.0,
    ) -> None:
        self.server = server
        self.token = token
        self.slot_n = slot_n
        self.agent_id = agent_id
        self.hostname = hostname
        self.heartbeat_s = heartbeat_s
        self.rule_cache = RuleCache(server)
        self._scopes: dict[str, anyio.CancelScope] = {}  # req_id → 取消域（收 Cancel 帧就地取消该任务）

    async def run_once(self) -> None:
        """连接一次并进入收发循环（连接关闭即返回）。"""
        async with websockets.connect(
            _ws_url(self.server), additional_headers={"authorization": f"Bearer {self.token}"}
        ) as ws:
            await ws.send(
                RegisterReq(
                    agent_id=self.agent_id,
                    hostname=self.hostname,
                    capabilities=Capabilities(),
                    slot_n=self.slot_n,
                ).model_dump_json()
            )
            await ws.recv()  # register_ack（M1 不深校验；M2 起校验契约版本）
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._heartbeat_loop, ws)
                async for message in ws:
                    frame = _server_frame.validate_json(message)
                    if isinstance(frame, TaskAssign):
                        tg.start_soon(self._handle_task, ws, frame)
                    elif isinstance(frame, Cancel) and frame.req_id:
                        scope = self._scopes.get(frame.req_id)  # 取消对应任务协程；回报由 _handle_task 发
                        if scope is not None:
                            scope.cancel()
                tg.cancel_scope.cancel()

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await anyio.sleep(self.heartbeat_s)
            inflight = list(self._scopes)  # 诚实自报：真实在途 req_id 与空闲槽
            await ws.send(
                Heartbeat(free_slots=max(0, self.slot_n - len(inflight)), inflight=inflight).model_dump_json()
            )

    async def _handle_task(self, ws, assign: TaskAssign) -> None:
        req_id = assign.task.req_id
        scope = anyio.CancelScope()
        self._scopes[req_id] = scope
        try:
            with scope:  # 独立取消域：收 Cancel(req_id) 帧只取消本任务，不影响其它/连接
                try:
                    report = await process_task(
                        assign.task,
                        upload_token=assign.upload_token,
                        server_base=self.server,
                        rule_cache=self.rule_cache,
                        agent_id=self.agent_id,
                    )
                    await ws.send(report.model_dump_json())
                except Exception as exc:  # noqa: BLE001  单任务失败回错误帧，不拖垮连接
                    await ws.send(
                        ErrorFrame(code=int(ErrorCode.SOFT_FAIL), message=str(exc), req_id=req_id).model_dump_json()
                    )
        finally:
            self._scopes.pop(req_id, None)
        if scope.cancel_called:  # 被取消：域外回报 CANCELED（域内 send 会被取消掉）
            await ws.send(
                StatusReport(req_id=req_id, state=int(RequestState.CANCELED), message="canceled").model_dump_json()
            )

    async def run(self, *, max_retries: int | None = None) -> None:
        """断线指数退避重连（简版）。max_retries=None 表示无限重连。"""
        attempt = 0
        while True:
            try:
                await self.run_once()
                attempt = 0
            except Exception:  # noqa: BLE001  连接层异常 → 退避重连
                if max_retries is not None and attempt >= max_retries:
                    raise
                attempt += 1
                await anyio.sleep(backoff_delay(attempt, base=1.0, cap=30.0, jitter=True))
