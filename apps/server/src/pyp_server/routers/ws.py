"""agent 接入 WebSocket 端点（M1：register→心跳→领任务→回报 完整循环）。

只传小消息（KB 级）；大对象走数据面直传。收到 ResultReport → 分流入库（core.run）。帧 schema 见 contracts.agent。
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from payipa.crawl.ingest import build_data_table
from payipa.crawl.run import (
    finalize_batch_if_done,
    handle_result,
    resolve_ingest_context,
    set_request_state,
)
from payipa.db.engine import get_engine
from payipa_contracts import (
    ClientFrame,
    ErrorFrame,
    Heartbeat,
    RegisterAck,
    RegisterReq,
    ResultBatch,
    ResultReport,
    StatusReport,
    is_compatible,
)
from pydantic import TypeAdapter, ValidationError

router = APIRouter()
_client_frame = TypeAdapter(ClientFrame)

_CLOSE_OK = 1000
_CLOSE_PROTOCOL = 1002
_CLOSE_UNSUPPORTED = 1003


async def _ingest_result(result: ResultBatch) -> None:
    """先写 data_center（指纹幂等）→ 再置 pyp 请求成功 → 尝试收尾批次。"""
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    uuid, fingerprint_keys, indexed = await resolve_ingest_context(pyp, int(result.req_id))
    table = build_data_table(uuid, indexed)
    await handle_result(pyp, dc, table, result, fingerprint_keys=fingerprint_keys)
    await finalize_batch_if_done(pyp, int(result.batch_id))


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
    hub.register(agent_id, ws, register.slot_n)
    await ws.send_text(RegisterAck(node_token="m1-stub-token").model_dump_json())  # M2：换取长期凭证

    try:
        while True:
            frame = _client_frame.validate_python(await ws.receive_json())
            if isinstance(frame, Heartbeat):
                hub.update_heartbeat(agent_id, frame.free_slots, frame.inflight)
            elif isinstance(frame, ResultReport):
                await _ingest_result(frame.result)
                hub.on_finished(agent_id, frame.result.req_id)
            elif isinstance(frame, StatusReport) and frame.state < 0:
                await set_request_state(get_engine("pyp"), int(frame.req_id), frame.state)
                hub.on_finished(agent_id, frame.req_id)
            elif isinstance(frame, ErrorFrame) and frame.req_id:
                await set_request_state(get_engine("pyp"), int(frame.req_id), frame.code)
                hub.on_finished(agent_id, frame.req_id)
    except WebSocketDisconnect:
        pass
    finally:
        if agent_id:
            hub.unregister(agent_id)
