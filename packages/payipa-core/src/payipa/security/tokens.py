"""内部上传令牌（HMAC 签名，短时效，绑定 source+batch）。

agent 上传 raw 到 local 兜底端点时携带；主控本地校验。KEK/密钥从 env 注入、不入库（SDD §10）。
M2 起可复用同机制签发 job_token（Query/LLM Gateway）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def new_node_token() -> tuple[str, str]:
    """签发长期节点凭证：返回 (明文, sha256 十六进制 hash)。

    明文只在 RegisterAck 里下发一次；库里只存 hash（SDD 红线9：token 存 hash、脚本不接触明文）。
    """
    plain = secrets.token_urlsafe(32)
    return plain, hashlib.sha256(plain.encode()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_upload_token(secret: str, source_uuid: str, batch_id: int | str, *, ttl_s: int = 3600) -> str:
    payload = {"s": source_uuid, "b": str(batch_id), "exp": int(time.time()) + ttl_s}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_upload_token(secret: str, token: str, *, now: int | None = None) -> dict | None:
    """校验签名与有效期；通过返回 claims，否则 None。"""
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
    if payload.get("exp", 0) < (now if now is not None else int(time.time())):
        return None
    return payload
