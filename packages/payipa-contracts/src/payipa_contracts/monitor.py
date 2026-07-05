"""监控/遥测 schema（主控侧聚合的全局视图）。

agent 只回报原始计数（见 result.ExecSummary），聚合在主控 core.monitor。接口本体在 apps/server，
页在 06 系统监控（SSE 实时）。M0 形状就位，消费（页面/聚合）在 M5。
"""

from __future__ import annotations

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


class ProxyStat(BaseModel):
    """代理出口统计（喂 07 调频与 monitor）。"""

    by_egress_domain: dict[str, float] = active("每 (出口×域) 成功率", default_factory=dict, since="M5")
