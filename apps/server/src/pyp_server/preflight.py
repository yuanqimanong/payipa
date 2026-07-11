"""启动前安全校验：production 模式拒绝不安全配置（dev 默认密钥 / RBAC 关闭）。

dev 模式（默认）宽松：仅在 API 免登录时打一条醒目告警。production 模式（PYP_SERVER_ENVIRONMENT=production）
严格：session_secret / upload_secret / cred_kek / agent_join_token 仍为 dev 默认或过短、或 RBAC 未开，
一律 RuntimeError 拒绝启动——把"忘了配密钥就上线"从隐患变成开机即失败。
"""

from __future__ import annotations

import logging

from payipa.db.settings import Settings as DbSettings
from payipa.db.settings import get_settings as get_db_settings

from pyp_server.settings import DEV_SESSION_SECRET, MIN_SESSION_SECRET_BYTES, ServerSettings, get_server_settings

logger = logging.getLogger("pyp_server.preflight")

# 各 dev 默认密钥（与 settings 默认值对齐；production 模式拒绝）。
_DEV_UPLOAD_SECRET = "dev-insecure-change-me"
_DEV_CRED_KEK = "dev-insecure-kek-change-me"
_DEV_JOIN_TOKEN = "dev"


def _production_problems(s: ServerSettings, db: DbSettings) -> list[str]:
    """收集 production 模式下的不安全配置项（空列表 = 通过）。"""
    problems: list[str] = []
    if not s.rbac_enabled:
        problems.append("PYP_SERVER_RBAC_ENABLED 必须为 true（生产须开权限闸门，否则 JSON API 免登录开放）")
    if s.session_secret == DEV_SESSION_SECRET:
        problems.append("PYP_SERVER_SESSION_SECRET 仍为 dev 默认值，会话可被伪造")
    if len(s.session_secret.encode()) < MIN_SESSION_SECRET_BYTES:
        problems.append(f"PYP_SERVER_SESSION_SECRET 少于 {MIN_SESSION_SECRET_BYTES} 字节（HS256 强度不足）")
    if s.agent_join_token == _DEV_JOIN_TOKEN:
        problems.append("PYP_SERVER_AGENT_JOIN_TOKEN 仍为 dev 默认值，任意 agent 可接入")
    if db.upload_secret == _DEV_UPLOAD_SECRET:
        problems.append("UPLOAD_SECRET 仍为 dev 默认值，内部上传/作业令牌可被伪造")
    if db.cred_kek == _DEV_CRED_KEK:
        problems.append("CRED_KEK 仍为 dev 默认值，凭证信封主密钥不安全")
    return problems


def run_preflight(server_settings: ServerSettings | None = None, db_settings: DbSettings | None = None) -> None:
    """启动时调用。production 模式有问题即抛 RuntimeError；dev 模式仅在 API 开放时告警。"""
    s = server_settings or get_server_settings()
    db = db_settings or get_db_settings()
    if s.environment == "production":
        problems = _production_problems(s, db)
        if problems:
            raise RuntimeError(
                "生产环境（PYP_SERVER_ENVIRONMENT=production）安全前置校验失败，拒绝启动：\n  - "
                + "\n  - ".join(problems)
                + "\n请在 .env / 环境变量注入真实密钥并开启 RBAC 后重启。"
            )
        logger.info("preflight: production security checks passed")
    elif not s.rbac_enabled:
        logger.warning(
            "⚠️ RBAC 关闭（dev 模式）：JSON API 无需登录即可访问，仅用于开发。"
            "生产请置 PYP_SERVER_ENVIRONMENT=production + PYP_SERVER_RBAC_ENABLED=true。"
        )
