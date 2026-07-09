"""签名不透明游标（M3 网关翻页）：HMAC-SHA256 签名，沙箱不可伪造/篡改。

游标载荷 = {a: after_id, c: 已消费行数, j: job jti, s: source}。绑定 jti+source：不能跨作业/跨源重用；
「已消费行数」随游标累计，使无状态网关也能强制 job_token 的行数配额（资源限额第 2 层）。
编码 = b64(json).b64(hmac)，与 security.tokens 同风格。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_cursor(secret: str, *, after_id: int, consumed: int, jti: str, source: str) -> str:
    body = _b64(json.dumps({"a": after_id, "c": consumed, "j": jti, "s": source}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_cursor(secret: str, token: str, *, jti: str, source: str) -> dict | None:
    """验签 + 绑定校验（jti/source 必须与当前令牌/请求一致）；通过返回 {a, c}，否则 None。"""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError, json.JSONDecodeError:
        return None
    if payload.get("j") != jti or payload.get("s") != source:
        return None  # 跨作业/跨源重用 → 拒
    return {"a": int(payload.get("a", 0)), "c": int(payload.get("c", 0))}
