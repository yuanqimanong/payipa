"""`pyp` 平台库表模型（SDD §4.1）。系统元数据 + 操作日志。

枚举值以字符串存（保持加法演进灵活）；state 用 smallint（正=正常态、负=错误码）。
加密列（api_config/config/secret/target_creds 等）存**密文**（envelope encryption，KEK 不入库）。
token 类（key_hash/node token）存 **hash 不可逆**。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from payipa.db.base import OwnedMixin, PypBase, TimestampMixin


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


# ══ RBAC / 用户 / 审计 ══════════════════════════════════════════════════════
class User(TimestampMixin, PypBase):
    __tablename__ = "users"

    id: Mapped[int] = _pk()
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # argon2
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/disabled（停用不删）
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Role(TimestampMixin, PypBase):
    __tablename__ = "roles"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # 管理员/技术/运营/运维
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserRole(PypBase):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)


class Permission(TimestampMixin, PypBase):
    __tablename__ = "permissions"

    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # 如 sql_query/force_insert
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserPermission(PypBase):
    __tablename__ = "user_permissions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id"), primary_key=True)


class RolePermission(PypBase):
    """角色→权限（标准 RBAC：用户经角色获得权限，另可经 user_permissions 直授）。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id"), primary_key=True)


class AuditLog(TimestampMixin, PypBase):
    __tablename__ = "audit_log"

    id: Mapped[int] = _pk()
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # web/api/system


# ══ 数据源 / 规则 ═══════════════════════════════════════════════════════════
class Source(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "sources"

    id: Mapped[int] = _pk()
    uuid: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # 分表短码来源
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    connector_type: Mapped[str] = mapped_column(String(16), default="web")  # web/api/feed/push/file_db
    channel_default: Mapped[str] = mapped_column(String(8), default="prod")
    proxy_config_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    agent_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rate_limit: Mapped[int] = mapped_column(Integer, default=10)  # 请求/秒（子域天花板，UI 可改）
    retry: Mapped[int] = mapped_column(Integer, default=3)
    timeout: Mapped[int] = mapped_column(Integer, default=30)
    raw_archive: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否开启 raw 存档


class Rule(TimestampMixin, PypBase):
    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("source_id", "version"),)

    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # 内容寻址
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/testing/active
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 选择器/清洗/版型识别
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


# ══ 任务 / 调度 / 批次 / 请求 ═══════════════════════════════════════════════
class Task(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "tasks"

    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(16), default="manual")  # manual/cron/once/api
    params: Mapped[dict] = mapped_column(JSONB, default=dict)  # 运行参数（能力参数化）
    priority: Mapped[str] = mapped_column(String(8), default="mid")  # high/mid/low
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chain_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)  # 链式上游


class Schedule(PypBase):
    __tablename__ = "schedules"

    id: Mapped[int] = _pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(128), nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Batch(TimestampMixin, PypBase):
    __tablename__ = "batches"

    id: Mapped[int] = _pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(8), default="prod")  # test/prod
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed/canceled
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)  # 总/成/败/进/空白率


class Request(TimestampMixin, PypBase):
    __tablename__ = "requests"

    id: Mapped[int] = _pk()
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[int] = mapped_column(SmallInteger, default=0)  # 正=正常态、负=错误码
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    url_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # URL 指纹：批内去重（唯一索引见迁移）
    # 见迁移 c1d2e3f4a5b6 的唯一索引 uq_requests_batch_url_hash (batch_id, url_hash)
    # 每请求解析计数（agent ExecSummary 回报，handle_result 回填）+ 耗时——喂 core.monitor 数据质量/时延（M5）
    count_ok: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 解析成功条数
    count_fail: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 解析失败条数
    count_blank: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 空白（无内容）条数
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 执行耗时（毫秒）


class TaskEvent(TimestampMixin, PypBase):
    __tablename__ = "task_events"

    id: Mapped[int] = _pk()
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class Agent(TimestampMixin, PypBase):
    __tablename__ = "agents"

    id: Mapped[int] = _pk()
    agent_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict)  # {automation: bool, ...}
    slot_n: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="offline")
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    node_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 存 hash
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ══ 代理池 ══════════════════════════════════════════════════════════════════
class ProxyProvider(TimestampMixin, PypBase):
    __tablename__ = "proxy_providers"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # tunnel/longlived/iplist
    api_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 加密存储
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ProxyConfig(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "proxy_configs"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), default="single")  # mix/single
    provider_refs: Mapped[dict] = mapped_column(JSONB, default=dict)


class ProxyUsage(TimestampMixin, PypBase):
    __tablename__ = "proxy_usage"

    id: Mapped[int] = _pk()
    egress_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ms
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Credential(TimestampMixin, PypBase):
    __tablename__ = "credentials"

    id: Mapped[int] = _pk()
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储或 hash（脚本不接触明文）
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)


# ══ 推送 / 对外 / 通知 ══════════════════════════════════════════════════════
class ApiKey(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "api_keys"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)  # 存 hash
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)  # 可读表清单
    quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class NotifyBot(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "notify_bots"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # lark/email/...
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 加密存储（用户级自管）


class PushComponent(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "push_components"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/testing/active
    code_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)  # 组件源码（固定方法 push(ctx)；内联存）
    allow_domains: Mapped[list] = mapped_column(JSONB, default=list)  # 目标域白名单（隔离子进程出网仅放行这些）
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)  # 发布签名（HMAC content_hash，红线7）
    target_creds: Mapped[str | None] = mapped_column(Text, nullable=True)  # 组件级加密存储（KEK 信封）


class PushOutbox(TimestampMixin, OwnedMixin, PypBase):
    """事务性 outbox（汲取点②）：退避 / 消费租约防重 / 死信 / 审计。"""

    __tablename__ = "push_outbox"

    id: Mapped[int] = _pk()
    component_id: Mapped[int] = mapped_column(ForeignKey("push_components.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Assembly(TimestampMixin, OwnedMixin, PypBase):
    __tablename__ = "assemblies"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    script_ver: Mapped[int] = mapped_column(Integer, default=1)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    upstream_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    incremental: Mapped[bool] = mapped_column(Boolean, default=False)
    # M3 slice-5：版本状态机 + 内容寻址 + 签名门 + 产物表配置
    product_code: Mapped[str | None] = mapped_column(String(32), nullable=True)  # asm_{product_code}
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 脚本内容寻址（版本 pin）
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/testing/active
    script_ref: Mapped[str | None] = mapped_column(Text, nullable=True)  # 脚本内容寻址引用（固定方法名）
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 发布签名（HMAC，执行器校验）
    fingerprint_keys: Mapped[list] = mapped_column(JSONB, default=list)  # 产物指纹字段
    indexed_fields: Mapped[list] = mapped_column(JSONB, default=list)  # 产物勾索引字段


class AssemblyWatermark(TimestampMixin, PypBase):
    """增量组装读侧水位（M3 slice-8）：某组装从某数据源已消费到的最大 data_* id。

    读腿可重算：清零该行即从头重读（写腿 asm_ 指纹幂等去重，故重读不产重复）。按 (assembly_id, source) 唯一。
    """

    __tablename__ = "assembly_watermarks"
    __table_args__ = (UniqueConstraint("assembly_id", "source", name="uq_assembly_watermark"),)

    id: Mapped[int] = _pk()
    assembly_id: Mapped[int] = mapped_column(ForeignKey("assemblies.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # data_{source} 的源短码
    position: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # 已消费到的最大 id


# ══ 公共配置 ════════════════════════════════════════════════════════════════
class LlmModel(TimestampMixin, PypBase):
    __tablename__ = "llm_models"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 凭证加密存储
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SystemPrompt(TimestampMixin, PypBase):
    __tablename__ = "system_prompts"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class GlobalParam(TimestampMixin, PypBase):
    __tablename__ = "global_params"

    id: Mapped[int] = _pk()
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)


class StorageConfig(TimestampMixin, PypBase):
    __tablename__ = "storage_config"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    backend: Mapped[str] = mapped_column(String(16), default="s3")  # s3/local
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 加密存储
