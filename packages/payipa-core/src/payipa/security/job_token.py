"""组装/查询作业令牌（job_token）：无状态 JWT，一次运行签发一个（04 定案）。

签名 + aud=job_id + exp=租约时长；scope = 可读表清单 + 行数配额（资源限额第 2 层）。沙箱脚本只持有 job_token
（零 DB 凭证），经 Query Gateway 取数时携带；主控 verify 后按 scope 授权/限额。jti 供 job_events/审计登记与
提前吊销（不落明文）。HS256 对称密钥从 env 注入（复用 upload_secret 域，不入库）。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt

_ALG = "HS256"


def issue_job_token(
    secret: str,
    job_id: str,
    *,
    tables: list[str],
    row_quota: int | None = None,
    lease_s: int = 1800,
    jti: str | None = None,
) -> tuple[str, str]:
    """签发 job_token；返回 (JWT 明文, jti)。jti 用于审计登记 + 吊销（存 jti/hash，不存明文 token）。"""
    now = int(time.time())
    jid = jti or secrets.token_urlsafe(9)
    payload: dict[str, Any] = {
        "aud": job_id,
        "iat": now,
        "exp": now + lease_s,
        "jti": jid,
        "scope": {"tables": list(tables), "row_quota": row_quota},
    }
    return jwt.encode(payload, secret, algorithm=_ALG), jid


def verify_job_token(secret: str, token: str, job_id: str) -> dict | None:
    """校验签名/有效期/受众(=job_id)；通过返回 claims，否则 None（过期/篡改/受众不符/格式错均归 None）。"""
    try:
        return jwt.decode(token, secret, algorithms=[_ALG], audience=job_id)
    except jwt.InvalidTokenError:
        return None


def decode_job_token(secret: str, token: str) -> dict | None:
    """网关侧校验：只验**签名 + 有效期**（不强求某个 aud——服务端签发即可信），返回 claims（含 aud/scope）。

    授权由 claims.scope（可读表白名单 + 行数配额）承担；aud=job_id 供审计/吊销定位。过期/篡改/格式错均归 None。
    """
    try:
        return jwt.decode(token, secret, algorithms=[_ALG], options={"verify_aud": False})
    except jwt.InvalidTokenError:
        return None


def token_allows_table(claims: dict, table: str) -> bool:
    """claims 的 scope 是否允许读该表（按 tables 白名单）。"""
    return table in (claims.get("scope") or {}).get("tables", [])
