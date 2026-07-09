"""对外 Dataset API 的 API Key（M4）：不透明随机秘密，只存 hash（红线9，明文只签发时下发一次）。

按行标记吊销（revoked）；scope 限定可读数据集（产物短码白名单）。校验 = 对presented key 求 sha256 查 key_hash。
"""

from __future__ import annotations

import hashlib
import secrets

_PREFIX = "pyp_"


def new_api_key() -> tuple[str, str]:
    """签发：返回 (明文, sha256 hash)。明文只此一次给用户，库里只存 hash。"""
    plain = _PREFIX + secrets.token_urlsafe(32)
    return plain, hash_api_key(plain)


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()
