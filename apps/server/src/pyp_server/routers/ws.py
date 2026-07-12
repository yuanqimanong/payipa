"""agent 接入 WebSocket 端点（M1：register→心跳→领任务→回报 完整循环）。

只传小消息（KB 级）；大对象走数据面直传。收到 ResultReport → 分流入库（core.run）。帧 schema 见 contracts.agent。
"""

from __future__ import annotations

import hmac
import logging

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from payipa.crawl.ingest import build_data_table
from payipa.crawl.run import (
    auth_node,
    commit_result,
    defer_request_for_retry,
    enroll_agent,
    finalize_batch_if_done,
    finalize_request_batch,
    mark_running,
    pause_source_for_request,
    register_agent,
    requeue_agent_inflight,
    resolve_ingest_context,
    set_agent_offline,
    set_request_state,
    touch_agent,
)
from payipa.db.engine import get_engine
from payipa.security.tokens import hash_token, new_node_token
from payipa_contracts import (
    Cancel,
    ClientFrame,
    ErrorCode,
    ErrorFrame,
    Heartbeat,
    RegisterAck,
    RegisterReq,
    RequestState,
    ResultAck,
    ResultBatch,
    ResultReport,
    StatusReport,
    TaskAck,
    is_compatible,
)
from pydantic import TypeAdapter, ValidationError

from pyp_server.ratelimit import SourceRateLimiter
from pyp_server.settings import get_server_settings
from pyp_server.triggers import on_batch_finalized

logger = logging.getLogger("pyp_server.ws")
router = APIRouter()
_client_frame = TypeAdapter(ClientFrame)
_reg_locks: dict[str, anyio.Lock] = {}  # 同 agent_id 注册串行化：并发重连不得互相覆盖凭证/顶掉更新的连接


def _reg_lock(agent_id: str) -> anyio.Lock:
    return _reg_locks.setdefault(agent_id, anyio.Lock())


_CLOSE_OK = 1000
_CLOSE_PROTOCOL = 1002
_CLOSE_UNSUPPORTED = 1003
_CLOSE_POLICY = 1008  # 策略违规（join token / 节点凭证不符）——agent 收到会作废本地凭证
_CLOSE_INTERNAL = 1011  # 服务端内部错误（生产注册落库失败 fail closed）
_CLOSE_RETRY = 1013  # 暂时不可用（认证查库失败等）——agent 保留凭证退避重连
_CLOSE_SUPERSEDED = 4001  # 同 id 新连接顶替（自定义应用码）


def _extract_bearer(header: str) -> str:
    """从 Authorization 头取 Bearer token（大小写不敏感前缀）；无前缀则原样返回。"""
    return header[7:] if header[:7].lower() == "bearer " else header


async def _ingest_result(result: ResultBatch, limiter: SourceRateLimiter, agent_id: str) -> bool:
    """多波续爬 + 入库 + 收尾。顺序：fencing 预检（迟到/越权结果整体丢弃，连发现链接也不入队）
    → 把本页发现链接并入同批入队（新 QUEUED 落库、防跨波提前收尾）
    → 写 data_center（指纹幂等）并置请求成功（事务内权威 fencing 再校验）
    → 尝试收尾批次 → AIMD 成功回升该源速率。"""
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    uuid, fingerprint_keys, indexed, channel = await resolve_ingest_context(pyp, int(result.req_id))
    table = build_data_table(uuid, indexed, channel)
    committed = await commit_result(pyp, dc, table, result, fingerprint_keys=fingerprint_keys, agent_id=agent_id)
    if not committed.accepted:
        logger.warning("stale result for req %s from %s (attempt=%s); dropped", result.req_id, agent_id, result.attempt)
        return False
    if await finalize_batch_if_done(pyp, int(result.batch_id)):  # 唯一那次 running→done
        await on_batch_finalized(int(result.batch_id))  # 链路自动推送 + 收尾通知（best-effort）
    limiter.on_ok(uuid)  # 成功 → AIMD 加性增
    return True


async def _pause_source(
    req_id: int,
    message: str | None,
    hub,
    *,
    response_status: int | None = None,
    reason_code: str | None = None,
) -> None:
    """暂停整源并取消同源其他在途任务。"""
    pyp = get_engine("pyp")
    _, inflight, batch_ids = await pause_source_for_request(
        pyp,
        req_id,
        message,
        response_status=response_status,
        reason_code=reason_code,
    )
    for other_req_id in inflight:
        conn = hub.find_by_req(other_req_id)
        if conn is not None:
            try:
                await hub.send_frame(conn.agent_id, Cancel(req_id=other_req_id))
            except Exception:  # noqa: BLE001
                logger.warning("cancel paused-source request %s failed", other_req_id, exc_info=True)
    for batch_id in batch_ids:
        if await finalize_batch_if_done(pyp, batch_id):
            await on_batch_finalized(batch_id)


async def _finalize_terminal_request(req_id: int) -> None:
    batch_id = await finalize_request_batch(get_engine("pyp"), req_id)
    if batch_id is not None:
        await on_batch_finalized(batch_id)


_RETRYABLE = frozenset(
    {int(ErrorCode.NETWORK), int(ErrorCode.TIMEOUT), int(ErrorCode.THROTTLED), int(ErrorCode.UPSTREAM)}
)


async def _defer_retry(frame: StatusReport | ErrorFrame, limiter: SourceRateLimiter, agent_id: str) -> bool:
    """持久化退避并同步进程内 AIMD；返回是否已重新排队。"""
    req_id = int(frame.req_id or 0)
    source, requeued = await defer_request_for_retry(
        get_engine("pyp"),
        req_id,
        frame.state if isinstance(frame, StatusReport) else frame.code,
        retry_after_s=frame.retry_after_s if isinstance(frame, StatusReport) else None,
        response_status=frame.response_status if isinstance(frame, StatusReport) else None,
        reason_code=frame.reason_code if isinstance(frame, StatusReport) else None,
        message=frame.message,
        max_attempt=get_server_settings().max_attempt,
        agent_id=agent_id,
        attempt=frame.attempt,
    )
    if source is not None:
        limiter.on_backoff_signal(
            source,
            retry_after_s=frame.retry_after_s if isinstance(frame, StatusReport) else None,
        )
    return requeued


@router.websocket("/ws/agent")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    settings = get_server_settings()
    bearer = _extract_bearer(ws.headers.get("authorization", ""))
    hub = ws.app.state.hub
    agent_id: str | None = None
    generation: int | None = None
    try:
        raw = await ws.receive_json()
    except ValueError, WebSocketDisconnect:
        await ws.close(code=_CLOSE_UNSUPPORTED)
        return

    try:
        register = _client_frame.validate_python(raw)
    except ValidationError:
        await ws.close(code=_CLOSE_UNSUPPORTED, reason="invalid frame")
        return
    if not isinstance(register, RegisterReq):
        await ws.close(code=_CLOSE_PROTOCOL, reason="expected register frame")
        return
    if not is_compatible(register.contract_version):
        await ws.close(code=_CLOSE_PROTOCOL, reason="contract version incompatible")
        return

    # 先读注册帧才能把长期凭证绑定到 agent_id。认证路径只有两条：已有长期凭证重连，或原子消费
    # UI 签发的一次性入网码。共享 join token 仅保留给 dev 本地兼容，production 永不接受。
    try:
        bound_agent = await auth_node(get_engine("pyp"), hash_token(bearer)) if bearer else None
    except Exception:  # noqa: BLE001
        logger.warning("auth_node lookup failed (DB down?); ask agent to retry", exc_info=True)
        await ws.close(code=_CLOSE_RETRY, reason="credential lookup unavailable, retry later")
        return
    if bound_agent is not None and register.agent_id != bound_agent:
        # 凭证与自报身份必须一致：凭证只能代表签发时绑定的节点
        await ws.close(code=_CLOSE_POLICY, reason="agent_id does not match node credential")
        return

    reg_id = register.agent_id
    issue_new = bound_agent is None  # 仅首次入网签发；凭证重连不轮换（否则旧凭证被无谓作废）
    async with _reg_lock(reg_id):  # 同 id 并发注册串行化：签发→落库→入 hub 是一个原子段
        token, token_hash = new_node_token() if issue_new else ("", None)  # 明文只此一次下发，库存 hash（红线9）
        try:
            if bound_agent is not None:
                registered = await register_agent(
                    get_engine("pyp"),
                    reg_id,
                    hostname=register.hostname,
                    slot_n=register.slot_n,
                    capabilities=register.capabilities.model_dump(),
                )
            else:
                registered = await enroll_agent(
                    get_engine("pyp"),
                    hash_token(bearer),
                    reg_id,
                    hostname=register.hostname,
                    slot_n=register.slot_n,
                    capabilities=register.capabilities.model_dump(),
                    node_token_hash=str(token_hash),
                )
                if (
                    registered is None
                    and settings.environment != "production"
                    and hmac.compare_digest(bearer, settings.agent_join_token)
                ):
                    # 本地开发兼容；register_agent 的凭证覆盖防护仍然生效。
                    registered = await register_agent(
                        get_engine("pyp"),
                        reg_id,
                        hostname=register.hostname,
                        slot_n=register.slot_n,
                        capabilities=register.capabilities.model_dump(),
                        node_token_hash=token_hash,
                    )
                if registered is None:
                    await ws.close(code=_CLOSE_POLICY, reason="invalid, expired, or already used enrollment token")
                    return
            weight, group_name = registered
        except PermissionError:
            await ws.close(code=_CLOSE_POLICY, reason="agent_id already has a node credential")
            return
        except Exception:  # noqa: BLE001
            # 凭证签发必须落库成功；任何环境都 fail closed，不能下发一个主控不认识的长期凭证。
            logger.error("register_agent %s failed; closing", reg_id, exc_info=True)
            await ws.close(code=_CLOSE_INTERNAL, reason="registration unavailable")
            return
        agent_id = reg_id
        generation, superseded = hub.register(
            agent_id,
            ws,
            register.slot_n,
            weight=weight,
            group_name=group_name,
            engines=register.capabilities.engines,
        )
    if superseded is not None:  # 同 id 重复连接：关旧留新（P0-08 连接代次）
        try:
            await superseded.ws.close(code=_CLOSE_SUPERSEDED, reason="superseded by newer connection")
        except Exception:  # noqa: BLE001 —— 旧连接可能已半开，关不上交给其 handler 的 finally
            logger.debug("close superseded connection of %s failed", agent_id, exc_info=True)
    await ws.send_text(RegisterAck(node_token=token).model_dump_json())

    try:
        while True:
            frame = _client_frame.validate_python(await ws.receive_json())
            if isinstance(frame, Heartbeat):
                hub.update_heartbeat(agent_id)  # 只刷新存活；槽位以 on_dispatched/on_finished 为准
                try:
                    await touch_agent(get_engine("pyp"), agent_id)  # last_heartbeat 落库（best-effort）
                except Exception:  # noqa: BLE001 —— DB 抖动不该断连接
                    logger.warning("touch_agent %s failed", agent_id, exc_info=True)
            elif isinstance(frame, TaskAck):
                try:  # ACK：ASSIGNED→RUNNING + ACK 短租展成执行租约；迟到 ack 记日志即可
                    if not await mark_running(
                        get_engine("pyp"),
                        int(frame.req_id),
                        agent_id,
                        frame.attempt,
                        lease_s=get_server_settings().task_lease_s,
                    ):
                        logger.info(
                            "stale task_ack for req %s from %s (attempt=%s)", frame.req_id, agent_id, frame.attempt
                        )
                except Exception:  # noqa: BLE001 —— DB 抖动不断连；租约 reaper 兜底
                    logger.warning("mark_running req %s failed", frame.req_id, exc_info=True)
            elif isinstance(frame, ResultReport):
                try:  # 单帧处理失败（如存量脏短码在入库时抛 ValueError）不得断连殃及全部在途
                    accepted = await _ingest_result(frame.result, ws.app.state.limiter, agent_id)
                    await ws.send_text(
                        ResultAck(
                            req_id=frame.result.req_id,
                            attempt=frame.result.attempt,
                            accepted=accepted,
                        ).model_dump_json()
                    )
                except Exception:  # noqa: BLE001 —— 记日志放行下一帧；该请求由租约 reaper 兜底
                    logger.exception("ingest result for req %s failed", frame.result.req_id)
                hub.on_finished(agent_id, frame.result.req_id)
            elif isinstance(frame, StatusReport) and (frame.state < 0 or frame.state == int(RequestState.CANCELED)):
                try:
                    if frame.state == int(ErrorCode.ACCESS_PAUSED):
                        await _pause_source(
                            int(frame.req_id),
                            frame.message,
                            hub,
                            response_status=frame.response_status,
                            reason_code=frame.reason_code,
                        )
                        terminal = True
                    elif frame.state in _RETRYABLE:
                        terminal = not await _defer_retry(frame, ws.app.state.limiter, agent_id)
                    else:
                        await set_request_state(
                            get_engine("pyp"),
                            int(frame.req_id),
                            frame.state,
                            agent_id=agent_id,
                            attempt=frame.attempt,
                            reason_code=frame.reason_code,
                            message=frame.message,
                        )
                        terminal = True
                except Exception:  # noqa: BLE001
                    logger.exception("handle status for req %s failed", frame.req_id)
                    terminal = False
                hub.on_finished(agent_id, frame.req_id)
                if terminal:
                    await _finalize_terminal_request(int(frame.req_id))
            elif isinstance(frame, ErrorFrame) and frame.req_id:
                try:
                    if frame.code == int(ErrorCode.ACCESS_PAUSED):
                        await _pause_source(int(frame.req_id), frame.message, hub)
                        terminal = True
                    elif frame.code in _RETRYABLE:
                        terminal = not await _defer_retry(frame, ws.app.state.limiter, agent_id)
                    else:
                        await set_request_state(
                            get_engine("pyp"),
                            int(frame.req_id),
                            frame.code,
                            agent_id=agent_id,
                            attempt=frame.attempt,
                            message=frame.message,
                        )
                        terminal = True
                except Exception:  # noqa: BLE001
                    logger.exception("handle error frame for req %s failed", frame.req_id)
                    terminal = False
                hub.on_finished(agent_id, frame.req_id)
                if terminal:
                    await _finalize_terminal_request(int(frame.req_id))
    except WebSocketDisconnect:
        pass
    finally:
        # 代次守卫（P0-08）：被新连接顶替的旧 handler 不得执行清理——否则会把在线节点标离线、
        # 把新连接的在途请求误回收。unregister 只在代次吻合时生效并返回 True。
        if agent_id and hub.unregister(agent_id, generation):
            # 断连即回收该 agent 在途请求 → 回 QUEUED（attempt+1），下一 tick 派发环重排到存活 agent；
            # 失败也不阻断清理，租约 reaper 兜底。
            try:
                await requeue_agent_inflight(get_engine("pyp"), agent_id, max_attempt=get_server_settings().max_attempt)
            except Exception:  # noqa: BLE001
                logger.warning("requeue inflight for %s failed; lease reaper will recover", agent_id, exc_info=True)
            try:
                await set_agent_offline(get_engine("pyp"), agent_id)  # 标记离线（保留权重/分组配置）
            except Exception:  # noqa: BLE001
                logger.warning("set_agent_offline %s failed", agent_id, exc_info=True)
            # 回收本 agent 的注册锁，避免 _reg_locks 随历史节点数只增不减地缓慢泄漏。
            # 仅在锁空闲（无并发重连正在注册）时删除：锁被持有 ⟹ 有任务在注册/等待 → 保留，防止删掉在用锁破坏互斥。
            lock = _reg_locks.get(agent_id)
            if lock is not None and not lock.locked():
                _reg_locks.pop(agent_id, None)
