"""启动前安全校验：production 模式拒绝不安全配置（dev 默认密钥 / RBAC 关闭）。

dev 模式（默认）宽松：仅在 API 免登录时打一条醒目告警。production 模式（PYP_SERVER_ENVIRONMENT=production）
严格：session_secret / bootstrap_token / upload_secret / cred_kek 仍为 dev 默认或过短、或 RBAC 未开，
一律 RuntimeError 拒绝启动——把"忘了配密钥就上线"从隐患变成开机即失败。

配置诚实（P0-16，两种模式都查）：配了未实现的 S3_* 或 REDIS_URL 都会开机即失败。
"""

from __future__ import annotations

import logging
import os

from payipa.db.settings import Settings as DbSettings
from payipa.db.settings import get_settings as get_db_settings
from payipa.storage import build_storage

from pyp_server.settings import (
    DEV_BOOTSTRAP_TOKEN,
    DEV_SESSION_SECRET,
    MIN_SESSION_SECRET_BYTES,
    ServerSettings,
    get_server_settings,
)

logger = logging.getLogger("pyp_server.preflight")

# 各 dev 默认密钥（与 settings 默认值对齐；production 模式拒绝）。
_DEV_UPLOAD_SECRET = "dev-insecure-upload-secret-change-me"
_DEV_CRED_KEK = "dev-insecure-kek-change-me"
# 凭证信封主密钥最小长度：KEK 保护所有下游凭证，且经单轮 SHA256 派生，弱/短口令型 KEK 抗离线暴力成本极低。
# 与 session_secret 同规格设长度门禁，避免「非默认但仍很弱」的 KEK 混进生产（推荐 32B 随机值）。
MIN_CRED_KEK_BYTES = 32


def _production_problems(s: ServerSettings, db: DbSettings) -> list[str]:
    """收集 production 模式下的不安全配置项（空列表 = 通过）。"""
    problems: list[str] = []
    if not s.rbac_enabled:
        problems.append("PYP_SERVER_RBAC_ENABLED 必须为 true（生产须开权限闸门，否则 JSON API 免登录开放）")
    if s.session_secret == DEV_SESSION_SECRET:
        problems.append("PYP_SERVER_SESSION_SECRET 仍为 dev 默认值，会话可被伪造")
    if len(s.session_secret.encode()) < MIN_SESSION_SECRET_BYTES:
        problems.append(f"PYP_SERVER_SESSION_SECRET 少于 {MIN_SESSION_SECRET_BYTES} 字节（HS256 强度不足）")
    if s.bootstrap_token == DEV_BOOTSTRAP_TOKEN:
        problems.append("PYP_SERVER_BOOTSTRAP_TOKEN 仍为 dev 默认值，首个管理员可被抢注")
    if len(s.bootstrap_token.encode()) < 24:
        problems.append("PYP_SERVER_BOOTSTRAP_TOKEN 少于 24 字节")
    allowed_hosts = [host.strip() for host in s.allowed_hosts.split(",") if host.strip()]
    if not allowed_hosts or any("*" in host for host in allowed_hosts):
        problems.append("PYP_SERVER_ALLOWED_HOSTS 必须填写生产域名或 IP，不能使用 *")
    if db.upload_secret == _DEV_UPLOAD_SECRET:
        problems.append("UPLOAD_SECRET 仍为 dev 默认值，内部上传/作业令牌可被伪造")
    if db.cred_kek == _DEV_CRED_KEK:
        problems.append("CRED_KEK 仍为 dev 默认值，凭证信封主密钥不安全")
    elif len(db.cred_kek.encode()) < MIN_CRED_KEK_BYTES:
        problems.append(f"CRED_KEK 少于 {MIN_CRED_KEK_BYTES} 字节（凭证信封主密钥抗离线暴力强度不足，建议 32B 随机值）")
    return problems


def _reject_multi_worker(s: ServerSettings) -> None:
    """环境变量声明了多 worker（WEB_CONCURRENCY/UVICORN_WORKERS>1）即拒绝启动。

    Hub/限流器是进程内状态，多 worker 会连接分片、限流倍增、调度与 Outbox 竞争（P0-09）。
    `uvicorn --workers N` 不设环境变量，这层只是廉价预检；权威互斥在 lifespan 的 PG advisory lock。
    """
    if not s.single_worker_guard:
        return
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.environ.get(var, "")
        try:
            n = int(raw)
        except ValueError:
            continue
        if n > 1:
            raise RuntimeError(f"payipa v1 只支持单 worker（{var}={n}）；请设 workers=1 或去掉该环境变量")


def run_preflight(server_settings: ServerSettings | None = None, db_settings: DbSettings | None = None) -> None:
    """启动时调用。production 模式有问题即抛 RuntimeError；dev 模式仅在 API 开放时告警。"""
    s = server_settings or get_server_settings()
    db = db_settings or get_db_settings()
    build_storage(db)  # 配置诚实：配了未实现的存储后端（S3_*）→ 开机即失败，而非首次上传才炸
    _reject_multi_worker(s)  # P0-09：v1 单实例单 worker 是硬约束，多 worker 环境变量开机即拒
    if db.redis_url:
        raise RuntimeError("REDIS_URL 尚未接线：当前队列以 PostgreSQL 为权威，拒绝接受不会生效的配置")
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
