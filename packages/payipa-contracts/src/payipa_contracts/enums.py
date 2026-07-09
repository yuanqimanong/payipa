"""跨进程/跨模块共用枚举。字符串枚举用 StrEnum（JSON 友好），状态用 IntEnum。"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class RequestState(IntEnum):
    """请求数据任务的正常态（正数）。负数为错误码，见 :mod:`payipa_contracts.errors`。

    转换：0→1→2→3；任意态收 Cancel → 4；失败置负码（未达 max_retry → 回 0 重试）。
    """

    QUEUED = 0  # 排队
    ASSIGNED = 1  # 已分派
    RUNNING = 2  # 运行中
    SUCCESS = 3  # 成功
    CANCELED = 4  # 已取消


class BatchStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class RuleStatus(StrEnum):
    """规则/版型版本状态机（内容寻址；draft/testing 仅测试任务引用，active 才被 prod 引用）。"""

    DRAFT = "draft"
    TESTING = "testing"
    ACTIVE = "active"


class Channel(StrEnum):
    """任务通道：test 产出落隔离测试集、不并入正式数据。"""

    TEST = "test"
    PROD = "prod"


class Priority(StrEnum):
    HIGH = "high"
    MID = "mid"
    LOW = "low"


class TriggerType(StrEnum):
    MANUAL = "manual"
    CRON = "cron"
    ONCE = "once"
    API = "api"


class ConnectorType(StrEnum):
    """源无关 Connector 五形态平权（汲取点④）；v1 落地 web + api，其余预留形态位。"""

    WEB = "web"
    API = "api"
    FEED = "feed"  # RSS/Atom（预留）
    PUSH = "push"  # webhook 接收（预留）
    FILE_DB = "file_db"  # 文件/DB（预留）


class EngineHint(StrEnum):
    """抓取引擎提示（三层反检测）。browser 需 agent 自动化能力分组。"""

    HTTP = "http"  # niquests
    IMPERSONATE = "impersonate"  # curl_cffi（TLS/JA3 指纹）
    BROWSER = "browser"  # Playwright 兼容 / CloakBrowser


class FieldType(StrEnum):
    """字段类型：存储 / 新链 / 存储+新链。"""

    STORE = "store"
    LINK = "link"
    STORE_LINK = "store+link"


class LocatorType(StrEnum):
    XPATH = "xpath"
    CSS = "css"
    JSONPATH = "jsonpath"
    REGEX = "regex"


class CrawlStrategy(StrEnum):
    BFS = "bfs"  # 广度（默认，可见总任务量）
    DFS = "dfs"  # 深度


class StorageBackend(StrEnum):
    S3 = "s3"
    LOCAL = "local"  # 主控本地盘兜底


class ArtifactStatus(StrEnum):
    PENDING = "pending"  # 已登记、上传中
    UPLOADED = "uploaded"  # agent 报告上传完成
    VERIFIED = "verified"  # 主控 HeadObject 复核通过
    FAILED = "failed"


class FilterOp(StrEnum):
    """Query Gateway 结构化过滤算子（无 SQL 串；M3 组装取数）。"""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"  # 子串包含（文本 LIKE %v%）
