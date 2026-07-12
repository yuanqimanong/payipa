"""server 级配置（pydantic-settings）。基础设施配置（PG 三库等）来自 payipa.db.get_settings。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# dev 默认密钥（production 模式下 preflight 校验拒绝这些值）。
DEV_SESSION_SECRET = "dev-session-secret-change-me-in-production-please"
MIN_SESSION_SECRET_BYTES = 32
DEV_BOOTSTRAP_TOKEN = "dev"


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
    # 空库创建首个管理员时必须提交；pypctl init 生成随机值，避免公开 /setup 被抢注。
    bootstrap_token: str = DEV_BOOTSTRAP_TOKEN
    session_ttl_s: int = 7 * 24 * 3600  # 会话有效期
    build_commit: str = ""  # 构建 commit（正式镜像注入；空=dev，/version 兜底 git rev-parse）
    build_time: str = ""  # 构建时间（正式镜像注入）
    # 逗号分隔 Host 白名单；"*" 仅适合开发。生产部署应填写实际域名/IP。
    allowed_hosts: str = "*"
    # v1 单实例守卫（P0-09）：后台环启动前须拿到 pyp 库 advisory lock；多 worker/多实例拒绝启动。
    # Hub/限流器是进程内状态，多 worker 会连接分片、限流倍增、调度与 Outbox 竞争——显式关闭后果自负。
    single_worker_guard: bool = True
    # 仅供 dev 本地兼容的一次性接入替代。production 完全忽略此共享值，只接受 UI 签发的一次性入网码。
    agent_join_token: str = "dev"

    # ── M5 RBAC ────────────────────────────────────────────────────────
    # JSON API 权限闸门开关。默认关（保持现网开放行为、不破坏既有用例）；生产播种角色后置 True 启用。
    # 关时 require_perm 直通（连登录都不强制）；开时未登录 401、缺权限 403，管理员(通配 *)放行一切。
    # production 模式强制为 True（preflight 校验），使全部 require_perm 端点登录+鉴权。
    rbac_enabled: bool = False

    # ── 内部上传（/internal/upload）──────────────────────────────────────
    max_upload_mb: int = 64  # 单次 raw 回传请求体上限（MB）：超限 413；流式读边读边校验，防大对象拖爆内存

    # ── M2 派发环（后台调度）─────────────────────────────────────────────
    dispatch_enabled: bool = True  # 后台派发环开关（测试关闭，避免与用例抢 QUEUED 请求）
    dispatch_interval_s: float = 1.0  # 派发/回收扫描间隔（秒）
    task_lease_s: int = 1800  # 执行租约（秒）：agent ACK 后展成此值；在途无终结超此即视为失联回收
    ack_timeout_s: int = 60  # ACK 短租（秒）：下发后 agent 未确认即被 reaper 快速回收重派（P0-10）。
    # 取 60 而非 30：结果帧与 ack 在同一 WS 上串行处理，重负载下 ack 可能排队，过短会误回收健康节点的任务。
    max_attempt: int = 3  # 请求最大尝试次数（含首次）；超过定格 NODE_LOST(-6)
    # raw/artifact 保留期回收（GC）在后台环内低频执行。此前 gc_expired_artifacts 已实现却从未被调度，
    # 配合「磁盘低水位即拒上传 + readyz 转 503」会演化成不可自愈的磁盘写满宕机（本值 ≤0 关闭 GC tick）。
    gc_interval_s: float = 300.0

    # ── M4 推送 Consumer（outbox 排空环）──────────────────────────────────
    push_enabled: bool = True  # 后台推送 Consumer 开关（测试关闭）
    push_interval_s: float = 2.0  # outbox 排空扫描间隔（秒）
    push_lease_s: int = 300  # 消费租约（秒）：inflight 超此视为消费者失联，回收重投
    push_max_attempts: int = 5  # 单条推送最大尝试次数；超过转 dead + 告警
    push_batch: int = 32  # 单轮最多领取/投递条数


@lru_cache
def get_server_settings() -> ServerSettings:
    return ServerSettings()
