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
    RegisterAck,
    RegisterReq,
    RequestState,
    ResultBatch,
    ResultReport,
    ServerFrame,
    StatusReport,
    TaskAck,
    TaskAssign,
    TaskSpec,
)
from pydantic import TypeAdapter

from pyp_agent.fetch import FetchNetworkError, FetchTimeout, FetchTooLarge, browser_available, fetch
from pyp_agent.interpret import interpret_page
from pyp_agent.response_policy import assess_response
from pyp_agent.rules import RuleCache
from pyp_agent.state import load_state, save_state
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
) -> ResultReport | StatusReport:
    """执行一个请求；访问被明确拒绝时不解析、不归档并请求主控暂停整源。"""
    started = time.monotonic()
    rule = await rule_cache.get(task.rule_ptr)
    try:
        fetched = await fetch(task.target, engine_hint=task.engine_hint, timeout=task.timeout_s)
    except FetchTimeout:
        return StatusReport(
            req_id=task.req_id,
            state=int(ErrorCode.TIMEOUT),
            message=f"request timed out after {task.timeout_s}s",
            reason_code="transport_timeout",
            retry_after_s=5.0,
            attempt=task.attempt,
        )
    except FetchNetworkError as exc:
        return StatusReport(
            req_id=task.req_id,
            state=int(ErrorCode.NETWORK),
            message=str(exc),
            reason_code="transport_error",
            retry_after_s=5.0,
            attempt=task.attempt,
        )
    except FetchTooLarge as exc:  # 超限即失败（不重试）：永久超大的页重试也没用，不进解析/归档
        return StatusReport(
            req_id=task.req_id,
            state=int(ErrorCode.SOFT_FAIL),
            message=str(exc),
            reason_code="response_too_large",
            attempt=task.attempt,
        )

    decision = assess_response(fetched.status, fetched.headers, fetched.body, fetched.content_type)
    if decision.outcome != "accept":
        return StatusReport(
            req_id=task.req_id,
            state=int(decision.error_code or ErrorCode.SOFT_FAIL),
            message=decision.message,
            response_status=fetched.status or None,
            reason_code=decision.reason_code,
            retry_after_s=decision.retry_after_s,
            attempt=task.attempt,
        )

    artifacts = []
    if upload_token and task.archive_raw:  # local 兜底：raw 经主控回传（S3 直传走 M5）
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
    # 数据质量原始计数（主控 core.monitor 聚合）：有非空字段=ok，全空=blank；
    # 解析失败在请求级以 PARSE_FAIL 状态体现，不在此计 count_fail。
    blank = sum(1 for it in parsed.items if not any(v not in (None, "", [], {}) for v in it.fields.values()))
    summary = ExecSummary(
        elapsed_s=round(time.monotonic() - started, 3),
        count_ok=len(parsed.items) - blank,
        count_fail=0,
        count_blank=blank,
        response_status=fetched.status or None,
        response_bytes=len(fetched.body),
        engine=task.engine_hint.value,
    )
    return ResultReport(
        result=ResultBatch(
            batch_id=task.batch_id,
            req_id=task.req_id,
            attempt=task.attempt,  # 代次回显：主控 fencing 据此拒绝迟到结果
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
        state_dir=None,
    ) -> None:
        self.server = server
        self.token = token  # join token：仅首次入网使用
        self.slot_n = slot_n
        self.agent_id = agent_id
        self.hostname = hostname
        self.heartbeat_s = heartbeat_s
        self.state_dir = state_dir  # 身份目录（None=不持久化，测试用）
        # 长期节点凭证（P0-07）：入网后由 RegisterAck 下发并持久化，重连凭它认证（空串=已作废）
        self.node_token: str | None = (load_state(state_dir).get("node_token") or None) if state_dir else None
        self.rule_cache = RuleCache(server)
        self._scopes: dict[str, anyio.CancelScope] = {}  # req_id → 取消域（收 Cancel 帧就地取消该任务）

    async def run_once(self) -> None:
        """连接一次并进入收发循环（连接关闭即返回）。"""
        bearer = self.node_token or self.token  # 有节点凭证用凭证（重连）；否则 join token（首次入网）
        async with websockets.connect(
            _ws_url(self.server), additional_headers={"authorization": f"Bearer {bearer}"}
        ) as ws:
            has_browser = browser_available()  # 装了 playwright extra 才上报 automation 能力（分组派发据此）
            await ws.send(
                RegisterReq(
                    agent_id=self.agent_id,
                    hostname=self.hostname,
                    capabilities=Capabilities(
                        automation=has_browser,
                        engines=["http", "browser"] if has_browser else ["http"],
                    ),
                    slot_n=self.slot_n,
                ).model_dump_json()
            )
            ack = _server_frame.validate_json(await ws.recv())
            if isinstance(ack, RegisterAck):
                if ack.heartbeat_interval_s > 0:
                    self.heartbeat_s = float(ack.heartbeat_interval_s)  # 采纳主控建议的心跳间隔
                if ack.node_token:  # 首次入网：持久化长期凭证，之后重连只用它（空串=沿用既有）
                    self.node_token = ack.node_token
                    if self.state_dir:
                        save_state(self.state_dir, node_token=ack.node_token)
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
        attempt = assign.task.attempt
        # 先 ACK（P0-10）：主控收到才把请求 ASSIGNED→RUNNING 并展成执行租约；丢失则被快速回收重派
        await ws.send(TaskAck(req_id=req_id, attempt=attempt).model_dump_json())
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
                        ErrorFrame(
                            code=int(ErrorCode.SOFT_FAIL), message=str(exc), req_id=req_id, attempt=attempt
                        ).model_dump_json()
                    )
        finally:
            self._scopes.pop(req_id, None)
        if scope.cancel_called:  # 被取消：域外回报 CANCELED（域内 send 会被取消掉）
            await ws.send(
                StatusReport(
                    req_id=req_id, state=int(RequestState.CANCELED), message="canceled", attempt=attempt
                ).model_dump_json()
            )

    async def run(self, *, max_retries: int | None = None) -> None:
        """断线指数退避重连（简版）。max_retries=None 表示无限重连。

        节点凭证被拒（策略关闭 1008，如管理员撤销）时丢弃本地凭证，退回 join token 重新入网。
        """
        attempt = 0
        while True:
            try:
                await self.run_once()
                attempt = 0
            except websockets.exceptions.ConnectionClosedError as exc:
                if self.node_token and exc.rcvd is not None and exc.rcvd.code == 1008:
                    self.node_token = None  # 凭证已失效：下次用 join token 重新换发
                    if self.state_dir:
                        save_state(self.state_dir, node_token="")
                if max_retries is not None and attempt >= max_retries:
                    raise
                attempt += 1
                await anyio.sleep(backoff_delay(attempt, base=1.0, cap=30.0, jitter=True))
            except Exception:  # noqa: BLE001  连接层异常 → 退避重连
                if max_retries is not None and attempt >= max_retries:
                    raise
                attempt += 1
                await anyio.sleep(backoff_delay(attempt, base=1.0, cap=30.0, jitter=True))
