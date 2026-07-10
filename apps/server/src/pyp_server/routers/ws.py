"""agent 接入 WebSocket 端点（M1：register→心跳→领任务→回报 完整循环）。

只传小消息（KB 级）；大对象走数据面直传。收到 ResultReport → 分流入库（core.run）。帧 schema 见 contracts.agent。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from payipa.crawl.ingest import build_data_table
from payipa.crawl.run import (
    enqueue_discovered,
    finalize_batch_if_done,
    finalize_request_batch,
    handle_result,
    pause_source_for_request,
    register_agent,
    requeue_agent_inflight,
    resolve_ingest_context,
    set_agent_offline,
    set_request_state,
    touch_agent,
)
from payipa.db.engine import get_engine
from payipa.security.tokens import new_node_token
from payipa_contracts import (
    Cancel,
    ClientFrame,
    ErrorCode,
    ErrorFrame,
    Heartbeat,
    RegisterAck,
    RegisterReq,
    RequestState,
    ResultBatch,
    ResultReport,
    StatusReport,
    is_compatible,
)
from pydantic import TypeAdapter, ValidationError

from pyp_server.ratelimit import SourceRateLimiter
from pyp_server.settings import get_server_settings
from pyp_server.triggers import on_batch_finalized

logger = logging.getLogger("pyp_server.ws")
router = APIRouter()
_client_frame = TypeAdapter(ClientFrame)

_CLOSE_OK = 1000
_CLOSE_PROTOCOL = 1002
_CLOSE_UNSUPPORTED = 1003


async def _ingest_result(result: ResultBatch, limiter: SourceRateLimiter) -> None:
    """多波续爬 + 入库 + 收尾。顺序：先把本页发现链接并入同批入队（新 QUEUED 落库、防跨波提前收尾）
    → 写 data_center（指纹幂等）并置请求成功 → 尝试收尾批次 → AIMD 成功回升该源速率。"""
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    uuid, fingerprint_keys, indexed = await resolve_ingest_context(pyp, int(result.req_id))
    table = build_data_table(uuid, indexed)
    await enqueue_discovered(pyp, int(result.req_id), result.discovered)
    await handle_result(pyp, dc, table, result, fingerprint_keys=fingerprint_keys)
    if await finalize_batch_if_done(pyp, int(result.batch_id)):  # 唯一那次 running→done
        await on_batch_finalized(int(result.batch_id))  # 链路自动推送 + 收尾通知（best-effort）
    limiter.on_ok(uuid)  # 成功 → AIMD 加性增


async def _pause_source(req_id: int, message: str | None, hub) -> None:
    """暂停整源并取消同源其他在途任务。"""
    pyp = get_engine("pyp")
    _, inflight, batch_ids = await pause_source_for_request(pyp, req_id, message)
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


@router.websocket("/ws/agent")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    hub = ws.app.state.hub
    agent_id: str | None = None
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

    agent_id = register.agent_id
    token, token_hash = new_node_token()  # 长期节点凭证：明文只此一次下发，库存 hash（红线9）
    try:  # 节点落库为 best-effort：PG 抖动/未起时仍允许注册（内存 hub + 默认权重/分组），不阻断握手
        weight, group_name = await register_agent(
            get_engine("pyp"),
            agent_id,
            hostname=register.hostname,
            slot_n=register.slot_n,
            capabilities=register.capabilities.model_dump(),
            node_token_hash=token_hash,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "register_agent %s failed (DB down?); registering in-memory with defaults", agent_id, exc_info=True
        )
        weight, group_name = 1, None
    hub.register(agent_id, ws, register.slot_n, weight=weight, group_name=group_name)
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
            elif isinstance(frame, ResultReport):
                await _ingest_result(frame.result, ws.app.state.limiter)
                hub.on_finished(agent_id, frame.result.req_id)
            elif isinstance(frame, StatusReport) and (frame.state < 0 or frame.state == int(RequestState.CANCELED)):
                if frame.state == int(ErrorCode.ACCESS_PAUSED):
                    await _pause_source(int(frame.req_id), frame.message, hub)
                else:
                    await set_request_state(get_engine("pyp"), int(frame.req_id), frame.state)
                hub.on_finished(agent_id, frame.req_id)
                await _finalize_terminal_request(int(frame.req_id))
            elif isinstance(frame, ErrorFrame) and frame.req_id:
                if frame.code == int(ErrorCode.ACCESS_PAUSED):
                    await _pause_source(int(frame.req_id), frame.message, hub)
                else:
                    await set_request_state(get_engine("pyp"), int(frame.req_id), frame.code)
                hub.on_finished(agent_id, frame.req_id)
                await _finalize_terminal_request(int(frame.req_id))
    except WebSocketDisconnect:
        pass
    finally:
        if agent_id:
            hub.unregister(agent_id)
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
