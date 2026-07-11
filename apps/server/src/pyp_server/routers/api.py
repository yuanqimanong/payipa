"""应用 REST API。/api/agents（在线节点）+ /api/sources/{uuid}/run（触发单源）+ 推送/通知 + 契约 stub。

完整端点清单见 SDD §6.5。敏感端点经 require_perm 权限闸门（M5 RBAC，settings.rbac_enabled 开关；
关时直通保持现网开放，开时按 payipa.security.rbac 权限矩阵放行/拒绝）。
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from payipa.crawl.run import batch_progress as compute_batch_progress
from payipa.crawl.run import cancel_batch as run_cancel_batch
from payipa.crawl.run import queue_depth as compute_queue_depth
from payipa.crawl.run import review_source_access
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.deliver.notify import NotifyError, notify
from payipa.deliver.outbox import enqueue_push
from payipa.monitor import node_metrics as compute_node_metrics
from payipa.monitor import source_health as compute_source_health
from payipa.monitor import system_overview as compute_system_overview
from payipa.security.audit import record_audit_best_effort
from payipa_contracts import (
    BatchProgress,
    Cancel,
    Channel,
    NodeMetric,
    NodeSnapshot,
    QueueStat,
    RulePack,
    SourceHealth,
    SystemOverview,
    TaskAssign,
    TaskSpec,
)
from pydantic import BaseModel, Field

from pyp_server.auth import get_current_user, require_perm
from pyp_server.service import dispatch_source_run

router = APIRouter(prefix="/api", tags=["api"])


class RunRequest(BaseModel):
    seed_urls: list[str] = Field(..., description="种子 URL 列表（每个产一条 request）")
    rule: RulePack = Field(..., description="本次运行使用的声明式规则")
    indexed_fields: list[str] = Field(default_factory=list, description="需索引字段（不填则取 rule 中 index=true）")
    channel: Channel = Channel.PROD


class RunResponse(BaseModel):
    batch_id: int
    requests: int
    dispatched: int


class AccessReviewRequest(BaseModel):
    access_basis: Literal["owned", "contracted", "public_policy"]
    access_reference: str = Field(..., min_length=1, max_length=2000)
    approved: bool
    reason: str | None = Field(None, max_length=1000)


class AccessReviewResponse(BaseModel):
    uuid: str
    approved: bool


class CancelResponse(BaseModel):
    canceled_queued: int = Field(..., description="就地取消的排队请求数")
    canceling_inflight: int = Field(..., description="已通知 agent 优雅收尾的在途请求数")


@router.get(
    "/agents",
    response_model=list[NodeSnapshot],
    summary="在线节点快照（来自 AgentHub）",
    dependencies=[Depends(require_perm("nodes.read"))],
)
async def list_agents(request: Request) -> list[NodeSnapshot]:
    return request.app.state.hub.snapshots()


@router.get(
    "/monitor/queue",
    response_model=QueueStat,
    summary="队列统计（QUEUED 排队深度，实时）",
    dependencies=[Depends(require_perm("monitor.read"))],
)
async def queue_stat() -> QueueStat:
    return QueueStat(by_priority=await compute_queue_depth(get_engine("pyp")))


@router.get(
    "/monitor/batches/{batch_id}",
    response_model=BatchProgress,
    summary="批次进度（按 state 实时聚合）",
    dependencies=[Depends(require_perm("monitor.read"))],
)
async def batch_progress(batch_id: int) -> BatchProgress:
    return BatchProgress(**await compute_batch_progress(get_engine("pyp"), batch_id))


@router.get(
    "/monitor/overview",
    response_model=SystemOverview,
    summary="系统监控总览（节点/队列/请求成败/整体数据质量，主控侧聚合）",
    dependencies=[Depends(require_perm("monitor.read"))],
)
async def monitor_overview() -> SystemOverview:
    return await compute_system_overview(get_engine("pyp"))


@router.get(
    "/monitor/nodes",
    response_model=list[NodeMetric],
    summary="各节点聚合指标（在线态/槽位 + 历史成败；运行态已占槽取自 AgentHub）",
    dependencies=[Depends(require_perm("monitor.read"))],
)
async def monitor_nodes(request: Request) -> list[NodeMetric]:
    live = {s.agent_id: s.slot_used for s in request.app.state.hub.snapshots()}
    return await compute_node_metrics(get_engine("pyp"), live_slots=live)


@router.get(
    "/monitor/sources",
    response_model=list[SourceHealth],
    summary="各数据源健康度（成败率 + 数据质量 + 错误码分布）",
    dependencies=[Depends(require_perm("monitor.read"))],
)
async def monitor_sources(request: Request) -> list[SourceHealth]:
    rows = await compute_source_health(get_engine("pyp"))
    limiter = request.app.state.limiter
    for row in rows:
        runtime = limiter.snapshot(row.source)
        if runtime is not None:
            row.effective_rate = round(runtime["effective_rate"], 3)
            row.retry_in_s = round(runtime["retry_in_s"], 1)
    return rows


@router.post("/tasks/preview", response_model=TaskAssign, summary="校验并回显 TaskSpec（演示契约，M0）")
async def preview_task(spec: TaskSpec) -> TaskAssign:
    return TaskAssign(task=spec)


@router.post(
    "/sources/{uuid}/run",
    response_model=RunResponse,
    summary="触发单源一次采集",
    dependencies=[Depends(require_perm("sources.run"))],
)
async def run_source(uuid: str, body: RunRequest) -> RunResponse:
    try:
        result = await dispatch_source_run(
            uuid=uuid,
            name=uuid,
            seed_urls=body.seed_urls,
            rule=body.rule,
            indexed_fields=body.indexed_fields,
            channel=body.channel,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse(**result)


@router.post(
    "/sources/{uuid}/access-review",
    response_model=AccessReviewResponse,
    summary="记录数据源访问复核并批准或暂停",
    dependencies=[Depends(require_perm("sources.write"))],
)
async def access_review(uuid: str, body: AccessReviewRequest, request: Request) -> AccessReviewResponse:
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="访问复核需要登录")
    pyp = get_engine("pyp")
    updated = await review_source_access(
        pyp,
        uuid,
        access_basis=body.access_basis,
        access_reference=body.access_reference,
        approved=body.approved,
        reason=body.reason,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"数据源 {uuid!r} 不存在")
    await record_audit_best_effort(
        pyp,
        action="source.access_review",
        actor_id=int(user["id"]),
        object_type="source",
        object_id=uuid,
        after={
            "access_basis": body.access_basis,
            "access_reference": body.access_reference,
            "approved": body.approved,
            "reason": body.reason,
        },
        source="api",
    )
    return AccessReviewResponse(uuid=uuid, approved=body.approved)


@router.post(
    "/batches/{batch_id}/cancel",
    response_model=CancelResponse,
    summary="取消一个批次（清排队 + 通知在途 agent 优雅收尾）",
    dependencies=[Depends(require_perm("tasks.cancel"))],
)
async def cancel_batch(batch_id: int, request: Request) -> CancelResponse:
    pyp = get_engine("pyp")
    inflight_ids, queued_ids = await run_cancel_batch(pyp, batch_id)
    hub = request.app.state.hub
    for req_id in inflight_ids:  # 逐条通知持有该 req 的 agent 取消（agent 取消协程树、回 CANCELED）
        conn = hub.find_by_req(req_id)
        if conn is not None:
            await hub.send_frame(conn.agent_id, Cancel(req_id=req_id))
    return CancelResponse(canceled_queued=len(queued_ids), canceling_inflight=len(inflight_ids))


# ── M4 推送/通知（手动触发；三触发统一走 outbox）─────────────────────────────
class PushEnqueueRequest(BaseModel):
    product_code: str | None = Field(None, description="数据集增量推送：推该产物短码的组装结果")
    rows: list[dict] | None = Field(None, description="内联推送：直接推这些行（详情页单条/多条按钮）")
    idempotency_key: str | None = Field(None, description="幂等键（同键只入一次）")


class PushEnqueueResponse(BaseModel):
    enqueued: int = Field(..., description="入队条数（0=幂等命中，已存在）")


class NotifyTestRequest(BaseModel):
    title: str = Field("payipa 测试通知", description="通知标题")
    text: str = Field("这是一条来自 payipa 的测试通知。", description="通知正文")


@router.post(
    "/push/components/{component_id}/enqueue",
    response_model=PushEnqueueResponse,
    summary="手动触发推送：把一次投递入 outbox（Consumer 隔离子进程投递）",
    dependencies=[Depends(require_perm("push.enqueue"))],
)
async def enqueue_push_api(component_id: int, body: PushEnqueueRequest) -> PushEnqueueResponse:
    if body.rows is not None:
        payload_ref = json.dumps({"kind": "inline", "rows": body.rows})
    elif body.product_code:
        payload_ref = json.dumps({"kind": "dataset", "product_code": body.product_code})
    else:
        payload_ref = None
    n = await enqueue_push(
        get_engine("pyp"), component_id=component_id, payload_ref=payload_ref, idempotency_key=body.idempotency_key
    )
    return PushEnqueueResponse(enqueued=n)


@router.post(
    "/notify/{bot_id}/test",
    summary="给通知机器人发一条测试通知",
    dependencies=[Depends(require_perm("push.manage"))],
)
async def notify_test(bot_id: int, body: NotifyTestRequest) -> dict:
    try:
        await notify(get_engine("pyp"), bot_id, title=body.title, text=body.text, kek=get_db_settings().cred_kek)
    except NotifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
