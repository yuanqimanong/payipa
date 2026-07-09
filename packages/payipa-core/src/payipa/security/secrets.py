"""凭证信封（KEK envelope，M4 slice-3a）：下游目标凭证 / 通知机器人 config 的对称加密存储。

红线9「凭证分层、脚本不接触明文」：明文凭证经 **KEK**（主密钥，仅主控 env 持有）用 Fernet
（AES-CBC + HMAC 认证加密）封装成密文入库；解密只发生在主控可信侧（推送/通知 Consumer），
解出的明文**仅注入隔离子进程**投递用（该子进程无 DB / 无 KEK）。密文被篡改 → 解密抛异常（认证失败）。

KEK 从 :class:`payipa.db.settings.Settings.cred_kek` 读（任意口令，内部 KDF 派生 32B Fernet key）；
生产走 env 注入。用户级通知机器人凭证同机制存储（用户自管、平台代管密钥）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from payipa.db.settings import get_settings


def _fernet(kek: str | None = None) -> Fernet:
    """从 KEK 口令派生 Fernet 实例（SHA256(kek) → urlsafe-b64 32B key）。缺省读 settings.cred_kek。"""
    secret = kek if kek is not None else get_settings().cred_kek
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str, *, kek: str | None = None) -> str:
    """封装明文凭证 → 密文（urlsafe b64 字符串）入库。"""
    return _fernet(kek).encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str, *, kek: str | None = None) -> str:
    """解封密文 → 明文（仅主控可信侧调用）。密文被篡改/KEK 不匹配 → 抛 InvalidToken。"""
    return _fernet(kek).decrypt(token.encode()).decode()


def encrypt_json(obj: Any, *, kek: str | None = None) -> str:
    """封装结构化凭证（dict/list，如 NotifyBot.config）→ 密文。"""
    return encrypt_secret(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), kek=kek)


def decrypt_json(token: str, *, kek: str | None = None) -> Any:
    """解封结构化凭证密文 → Python 对象。"""
    return json.loads(decrypt_secret(token, kek=kek))


__all__ = ["InvalidToken", "decrypt_json", "decrypt_secret", "encrypt_json", "encrypt_secret"]
