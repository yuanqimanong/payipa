"""事件 schema：审计事件与生命周期/血缘事件的形状（可观测/审计用）。

审计形状对应 pyp.audit_log 表（03 §2.5：actor/action/object/前后值/来源/时间）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from payipa_contracts._annotate import active


class AuditEvent(BaseModel):
    """操作审计事件（任务增删改跑停、规则发布、强制入库、SQL 查询、权限变更、交接迁移…）。"""

    actor: str = active("操作者（用户 id / 系统）", since="M2")
    action: str = active("动作，如 task.run / rule.publish / data.force_insert", since="M2")
    object_type: str = active("对象类型，如 task / rule / user", since="M2")
    object_id: str | None = active("对象 id", default=None, since="M2")
    before: dict[str, Any] | None = active("变更前关键值", default=None, since="M2")
    after: dict[str, Any] | None = active("变更后关键值", default=None, since="M2")
    source: str | None = active("来源（web/api/system）", default=None, since="M2")
    ts: float | None = active("事件时间 epoch 秒（缺省由服务端补）", default=None, since="M2")


class LifecycleEvent(BaseModel):
    """任务生命周期/血缘事件（喂 task_events 表与血缘可视化）。"""

    batch_id: str = active("批次 id（correlation）", since="M1")
    type: str = active("事件类型，如 batch.started / request.ingested / assembly.triggered", since="M1")
    payload: dict[str, Any] = active("事件载荷", default_factory=dict, since="M1")
    ts: float | None = active("事件时间 epoch 秒", default=None, since="M1")
