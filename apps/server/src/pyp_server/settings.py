"""server 级配置（pydantic-settings）。基础设施配置（PG 三库等）来自 payipa.db.get_settings。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    session_secret: str = "dev-session-secret-change-me-in-production-please"  # 生产走 env 注入（≥32B）
    session_ttl_s: int = 7 * 24 * 3600  # 会话有效期

    # ── M2 派发环（后台调度）─────────────────────────────────────────────
    dispatch_enabled: bool = True  # 后台派发环开关（测试关闭，避免与用例抢 QUEUED 请求）
    dispatch_interval_s: float = 1.0  # 派发/回收扫描间隔（秒）
    task_lease_s: int = 1800  # 任务租约（秒）：在途无终结超此即视为失联回收；对齐 TaskSpec.timeout_s 默认
    max_attempt: int = 3  # 请求最大尝试次数（含首次）；超过定格 NODE_LOST(-6)


@lru_cache
def get_server_settings() -> ServerSettings:
    return ServerSettings()
