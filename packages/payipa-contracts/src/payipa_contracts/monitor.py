"""监控/遥测 schema（主控侧聚合的全局视图）。

agent 只回报原始计数（见 result.ExecSummary），聚合在主控 core.monitor。接口本体在 apps/server，
页在 06 系统监控（SSE 实时）。M0 形状就位，消费（页面/聚合）在 M5。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from payipa_contracts._annotate import active


class NodeSnapshot(BaseModel):
    """节点状态快照。"""

    agent_id: str = active("节点 id", since="M2")
    online: bool = active("是否在线", since="M2")
    slot_n: int = active("并发槽容量 N", ge=0, since="M2")
    slot_used: int = active("已占用槽数", ge=0, since="M2")
    inflight: list[str] = active("在途请求任务 id 清单", default_factory=list, since="M2")


class QueueStat(BaseModel):
    """队列统计。"""

    by_priority: dict[str, int] = active("各优先级队列深度", default_factory=dict, since="M2")
    by_group: dict[str, int] = active("各分组队列深度", default_factory=dict, since="M2")


class BatchProgress(BaseModel):
    """批次进度。"""

    total: int = active("总请求任务数", ge=0, since="M1")
    ok: int = active("成功数", ge=0, since="M1")
    fail: int = active("失败数", ge=0, since="M1")
    running: int = active("进行中数", ge=0, since="M1")
    pct: float = active("完成百分比 0–100", ge=0, le=100, since="M1")


class QualityMetric(BaseModel):
    """数据质量指标（字段解析成功·失败·空白率）。"""

    parse_ok_rate: float = active("解析成功率 0–1", ge=0, le=1, since="M5")
    parse_fail_rate: float = active("解析失败率 0–1", ge=0, le=1, since="M5")
    blank_rate: float = active("空白率 0–1", ge=0, le=1, since="M5")


class NodeMetric(BaseModel):
    """单节点聚合指标（在线态 + 历史成败，主控侧从 agents + requests 汇总）。"""

    agent_id: str = active("节点 id", since="M5")
    online: bool = active("是否在线", since="M5")
    slot_n: int = active("并发槽容量 N", ge=0, since="M5")
    slot_used: int = active("已占用槽数（运行态）", ge=0, default=0, since="M5")
    ok: int = active("历史成功请求数", ge=0, default=0, since="M5")
    fail: int = active("历史失败请求数", ge=0, default=0, since="M5")
    success_rate: float | None = active("成功率 0–1（None=暂无样本）", ge=0, le=1, default=None, since="M5")


class SourceHealth(BaseModel):
    """单数据源健康度（成败率 + 数据质量），主控侧聚合。"""

    source: str = active("数据源短码", since="M5")
    name: str | None = active("数据源名称", default=None, since="M6")
    access_state: str = active("运行状态：active/cooling/paused/review", default="review", since="M6")
    pause_reason: str | None = active("人工暂停原因", default=None, since="M6")
    cooldown_until: datetime | None = active("自动冷却结束时间", default=None, since="M6")
    cooldown_reason: str | None = active("自动冷却原因码", default=None, since="M6")
    rate_limit: float = active("配置的额定请求速率", default=0, ge=0, since="M6")
    effective_rate: float | None = active("AIMD 当前有效速率", default=None, ge=0, since="M6")
    retry_in_s: float = active("进程内 Retry-After 剩余秒数", default=0, ge=0, since="M6")
    last_status_code: int | None = active("最近一次 HTTP 状态码", default=None, ge=100, le=599, since="M6")
    consecutive_failures: int = active("连续失败次数", default=0, ge=0, since="M6")
    last_success_at: datetime | None = active("最近成功时间", default=None, since="M6")
    last_failure_at: datetime | None = active("最近失败时间", default=None, since="M6")
    total: int = active("请求总数（终态）", ge=0, default=0, since="M5")
    ok: int = active("成功数", ge=0, default=0, since="M5")
    fail: int = active("失败数", ge=0, default=0, since="M5")
    success_rate: float | None = active("成功率 0–1（None=暂无样本）", ge=0, le=1, default=None, since="M5")
    quality: QualityMetric | None = active("数据质量（解析成功·失败·空白率）", default=None, since="M5")
    by_error: dict[str, int] = active("失败错误码分布（错误码字符串→计数）", default_factory=dict, since="M5")


class SystemOverview(BaseModel):
    """系统监控总览（一屏聚合：节点/队列/请求成败/数据质量）。"""

    nodes_online: int = active("在线节点数", ge=0, default=0, since="M5")
    nodes_total: int = active("已注册节点数", ge=0, default=0, since="M5")
    queue_depth: int = active("当前排队请求数（running 批次 QUEUED）", ge=0, default=0, since="M5")
    requests_total: int = active("请求总数（终态样本）", ge=0, default=0, since="M5")
    ok: int = active("成功数", ge=0, default=0, since="M5")
    fail: int = active("失败数", ge=0, default=0, since="M5")
    success_rate: float | None = active("整体成功率 0–1（None=暂无样本）", ge=0, le=1, default=None, since="M5")
    quality: QualityMetric | None = active("整体数据质量", default=None, since="M5")
