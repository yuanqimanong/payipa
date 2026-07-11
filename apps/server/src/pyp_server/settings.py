"""server 级配置（pydantic-settings）。基础设施配置（PG 三库等）来自 payipa.db.get_settings。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# dev 默认密钥（production 模式下 preflight 校验拒绝这些值）。
DEV_SESSION_SECRET = "dev-session-secret-change-me-in-production-please"
MIN_SESSION_SECRET_BYTES = 32


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PYP_SERVER_",
        env_file=".env",  # payipa/.env（本项目内）；见 .env.example
        env_file_encoding="utf-8",
        extra="ignore",
    )

    title: str = "payipa"
    version: str = "0.1.0"
    description: str = "payipa / 爬亿爬 —— 数据获取→清洗→查询→组装→推送 一站式平台（M0 骨架）"
    debug: bool = False
    # 运行环境：dev（默认，宽松：API 免登录、允许 dev 默认密钥）/ production（严：强制真密钥 + RBAC）。
    # 生产部署置 PYP_SERVER_ENVIRONMENT=production，启动前置校验（preflight.py）会拒绝不安全配置。
    environment: str = "dev"
    session_secret: str = DEV_SESSION_SECRET  # 生产走 env 注入（≥32B）；production 模式拒绝默认值
    session_ttl_s: int = 7 * 24 * 3600  # 会话有效期
    # agent 接入 join token（WS 握手 Authorization: Bearer）。默认 dev；production 模式拒绝默认值。
    agent_join_token: str = "dev"

    # ── M5 RBAC ────────────────────────────────────────────────────────
    # JSON API 权限闸门开关。默认关（保持现网开放行为、不破坏既有用例）；生产播种角色后置 True 启用。
    # 关时 require_perm 直通（连登录都不强制）；开时未登录 401、缺权限 403，管理员(通配 *)放行一切。
    # production 模式强制为 True（preflight 校验），使全部 require_perm 端点登录+鉴权。
    rbac_enabled: bool = False

    # ── M2 派发环（后台调度）─────────────────────────────────────────────
    dispatch_enabled: bool = True  # 后台派发环开关（测试关闭，避免与用例抢 QUEUED 请求）
    dispatch_interval_s: float = 1.0  # 派发/回收扫描间隔（秒）
    task_lease_s: int = 1800  # 任务租约（秒）：在途无终结超此即视为失联回收；对齐 TaskSpec.timeout_s 默认
    max_attempt: int = 3  # 请求最大尝试次数（含首次）；超过定格 NODE_LOST(-6)

    # ── M4 推送 Consumer（outbox 排空环）──────────────────────────────────
    push_enabled: bool = True  # 后台推送 Consumer 开关（测试关闭）
    push_interval_s: float = 2.0  # outbox 排空扫描间隔（秒）
    push_lease_s: int = 300  # 消费租约（秒）：inflight 超此视为消费者失联，回收重投
    push_max_attempts: int = 5  # 单条推送最大尝试次数；超过转 dead + 告警
    push_batch: int = 32  # 单轮最多领取/投递条数


@lru_cache
def get_server_settings() -> ServerSettings:
    return ServerSettings()
