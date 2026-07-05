"""agent 接入 WebSocket 端点（M0 空壳：仅注册握手 + 契约版本校验，随后关闭）。

M2 起进入心跳/派发/状态循环。帧 schema 在 payipa-contracts.agent。
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from payipa_contracts import ClientFrame, RegisterAck, RegisterReq, is_compatible
from pydantic import TypeAdapter, ValidationError

router = APIRouter()
_client_frame = TypeAdapter(ClientFrame)

# WS 关闭码
_CLOSE_OK = 1000
_CLOSE_PROTOCOL = 1002
_CLOSE_UNSUPPORTED = 1003


@router.websocket("/ws/agent")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    try:
        raw = await ws.receive_json()
    except ValueError:  # 非法 JSON（含 json.JSONDecodeError）
        await ws.close(code=_CLOSE_UNSUPPORTED, reason="invalid json")
        return

    try:
        frame = _client_frame.validate_python(raw)
    except ValidationError:
        await ws.close(code=_CLOSE_UNSUPPORTED, reason="invalid frame")
        return

    if not isinstance(frame, RegisterReq):
        await ws.close(code=_CLOSE_PROTOCOL, reason="expected register frame")
        return

    if not is_compatible(frame.contract_version):
        await ws.close(code=_CLOSE_PROTOCOL, reason="contract version incompatible")
        return

    # M0 空壳：不落库、不发真实节点凭证；M2 起换取长期 node_token（存 hash）并进入循环
    ack = RegisterAck(node_token="m0-stub-token")
    await ws.send_json(ack.model_dump())
    await ws.close(code=_CLOSE_OK)
