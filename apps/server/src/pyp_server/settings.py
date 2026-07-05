"""server 级配置（pydantic-settings）。基础设施配置（PG 三库等）来自 payipa.db.get_settings。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PYP_SERVER_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    title: str = "payipa"
    version: str = "0.1.0"
    description: str = "payipa / 爬亿爬 —— 数据获取→清洗→查询→组装→推送 一站式平台（M0 骨架）"
    debug: bool = False
    session_secret: str = "dev-session-secret-change-me-in-production-please"  # 生产走 env 注入（≥32B）
    session_ttl_s: int = 7 * 24 * 3600  # 会话有效期


@lru_cache
def get_server_settings() -> ServerSettings:
    return ServerSettings()
