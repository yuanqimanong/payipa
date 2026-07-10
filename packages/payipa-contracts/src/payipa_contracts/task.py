"""任务定义（TaskSpec）：主控下发给 agent 的调度单元（请求数据任务）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from payipa_contracts._annotate import active, reserved
from payipa_contracts.enums import Channel, EngineHint, Priority
from payipa_contracts.rule import RulePointer


class TaskSpec(BaseModel):
    """一个请求数据任务（恒小、可复现；规则走指针不内嵌）。"""

    task_id: str = active("用户视角任务 id")
    req_id: str = active("请求数据任务 id（调度单元；agent 回报 StatusReport/ResultBatch 以此为准）")
    batch_id: str = active("本次执行轮（批次）id")
    source: str = active("数据源 id/短码")
    target: str = active("抓取目标（URL / API endpoint）")
    rule_ptr: RulePointer = active("规则指针 (rule_id, version, content_hash)")
    channel: Channel = active("通道 test/prod（test 产出隔离、不入正式库）", default=Channel.PROD)
    priority: Priority = active("优先级（高插队）", default=Priority.MID)
    timeout_s: int = active("单任务硬超时（秒）", default=1800, gt=0)
    params: dict[str, Any] = active("运行参数（能力参数化：页数/范围等）", default_factory=dict)
    engine_hint: EngineHint = active("采集引擎提示；当前使用 http，browser 为可选能力", default=EngineHint.HTTP)
    # 以下为后续里程碑接线的预留位
    group: str | None = reserved("分发分组（test/prod 集群、自动化能力集群）", default=None, since="M2")
    account: str | None = reserved("采集账号/凭证维度（按账号限流）", default=None, since="M5")
