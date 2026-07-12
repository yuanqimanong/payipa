"""数据库/基础设施配置（pydantic-settings 读 .env 或环境变量）。

启动必需项走环境变量（SDD §3.5）。.env 在 project/ 根（payipa 的父目录），故同时探测
``.env`` 与 ``../.env``；生产由 compose/环境注入真实变量。库名可配（09 定案）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

DbKey = Literal["pyp", "data_center", "business"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",  # payipa/.env（本项目内）；见 .env.example
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # PostgreSQL（三库同实例，库名可配）
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_db_pyp: str = "pyp_sys"  # 平台库（.env 默认名 pyp_sys）
    pg_db_data_center: str = "data_center"  # 采集数据库
    pg_db_business: str = "business"  # 组装产物库
    db_null_pool: bool = False  # 测试/短命进程可关闭连接复用，避免跨事件循环持有 asyncpg 连接

    # 可选组件占位（均未实现）：redis_url 无任何消费方（队列走 PG）；s3_* 配置即拒绝启动
    # （storage.build_storage 报错，preflight 开机即失败）——绝不静默回退 local。
    redis_url: str | None = None
    s3_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str | None = None

    # 存储兜底（local 后端）+ 内部上传
    data_root: str = "var/storage"  # 本地对象存储根目录
    upload_secret: str = "dev-insecure-upload-secret-change-me"  # 内部上传 HMAC 密钥（生产走 env 注入）
    cred_kek: str = "dev-insecure-kek-change-me"  # 凭证信封主密钥（KEK，红线9）；生产走 env 注入，脚本永不接触
    min_free_mb: int = 500  # 磁盘水位下限（MB）：低于则拒绝新上传并告警
    raw_retention_days: int = 7  # raw 归档默认保留期（按源可覆盖，02 定案）；GC 清理过期

    def _db_name(self, key: DbKey) -> str:
        return {
            "pyp": self.pg_db_pyp,
            "data_center": self.pg_db_data_center,
            "business": self.pg_db_business,
        }[key]

    def _url(self, drivername: str, key: DbKey) -> URL:
        return URL.create(
            drivername=drivername,
            username=self.pg_user,
            password=self.pg_password,
            host=self.pg_host,
            port=self.pg_port,
            database=self._db_name(key),
        )

    def async_url(self, key: DbKey) -> URL:
        """asyncpg 驱动的连接 URL（运行时用）。"""
        return self._url("postgresql+asyncpg", key)

    def sync_url(self, key: DbKey) -> URL:
        """psycopg（同步）驱动的连接 URL（Alembic 迁移用）。"""
        return self._url("postgresql+psycopg", key)


@lru_cache
def get_settings() -> Settings:
    """进程内单例（首次访问时读取 env/.env）。"""
    return Settings()
