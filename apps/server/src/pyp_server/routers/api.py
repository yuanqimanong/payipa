"""契约 stub API（M0 空壳）：让 OpenAPI 呈现 payipa-contracts 的 schema。

端点返回空/回显，标注里程碑；真实逻辑在对应里程碑接入 core。完整端点清单见 SDD §6.5。
"""

from __future__ import annotations

from fastapi import APIRouter
from payipa_contracts import (
    BatchProgress,
    NodeSnapshot,
    QueueStat,
    TaskAssign,
    TaskSpec,
)

router = APIRouter(prefix="/api", tags=["stub (M1+)"])


@router.get("/agents", response_model=list[NodeSnapshot], summary="节点列表（M0 空壳，M2 接线）")
async def list_agents() -> list[NodeSnapshot]:
    return []


@router.get("/monitor/queue", response_model=QueueStat, summary="队列统计（M0 空壳，M2 接线）")
async def queue_stat() -> QueueStat:
    return QueueStat()


@router.get(
    "/monitor/batches/{batch_id}",
    response_model=BatchProgress,
    summary="批次进度（M0 空壳，M1 接线）",
)
async def batch_progress(batch_id: str) -> BatchProgress:
    return BatchProgress(total=0, ok=0, fail=0, running=0, pct=0.0)


@router.post(
    "/tasks/preview",
    response_model=TaskAssign,
    summary="校验并回显 TaskSpec（演示契约 round-trip，M0）",
)
async def preview_task(spec: TaskSpec) -> TaskAssign:
    return TaskAssign(task=spec)
