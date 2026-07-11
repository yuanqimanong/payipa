"""monitor —— 主控侧全局聚合（09 定案：core 无 agent 模块，agent 只回报原始计数，聚合在此）。

汇总三类指标（跨 agent + 任务 + 数据质量的全局视图）：
- **节点**：agents 在线态/槽位 + 历史成败（requests 按 agent_id 分组）→ NodeMetric；
- **数据源健康**：每源请求成败率 + 数据质量（解析成功·失败·空白率）→ SourceHealth；
- **系统总览**：节点/队列/请求成败/整体质量一屏 → SystemOverview。

数据质量来自 requests 的 count_ok/fail/blank（agent ExecSummary 回填，见 crawl/run.handle_result）；
无样本（NULL/0）时质量记 None、成功率记 1.0（诚实：没有负面信号）。接口在 apps/server，页在 06（SSE 实时）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from payipa_contracts import (
    NodeMetric,
    QualityMetric,
    RequestState,
    SourceHealth,
    SystemOverview,
    label,
)
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import Agent, Batch, Request, Source, Task

_SUCCESS = int(RequestState.SUCCESS)


def _rate(ok: int, total: int) -> float:
    """成功率：无样本记 1.0（无负面信号），否则 ok/total 保留 4 位。"""
    return 1.0 if total <= 0 else round(ok / total, 4)


def _quality(ok: int, fail: int, blank: int) -> QualityMetric | None:
    """由累计计数算质量指标；三者皆 0（无样本）返回 None。"""
    total = ok + fail + blank
    if total <= 0:
        return None
    return QualityMetric(
        parse_ok_rate=round(ok / total, 4),
        parse_fail_rate=round(fail / total, 4),
        blank_rate=round(blank / total, 4),
    )


#: 在线判定的心跳新鲜度阈值（秒）：status=online 但心跳超此视为失联（僵尸节点——
#: 主控被硬杀/agent 崩溃时 WS 断连处理器不会跑，status 列会永久停留 online）。
HEARTBEAT_FRESH_S = 90


def _node_online(status: str | None, last_heartbeat: datetime | None, *, now: datetime | None = None) -> bool:
    """节点在线 = 状态列 online 且心跳在新鲜期内（无心跳记录视为离线）。"""
    if status != "online" or last_heartbeat is None:
        return False
    beat = last_heartbeat if last_heartbeat.tzinfo else last_heartbeat.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - beat).total_seconds() <= HEARTBEAT_FRESH_S


async def node_metrics(engine_pyp: AsyncEngine, live_slots: dict[str, int] | None = None) -> list[NodeMetric]:
    """每节点聚合：agents 表在线态/槽位 + requests 历史成败（按 agent_id）。

    live_slots：运行态已占槽（来自 AgentHub 记账，DB 无此实时值）；缺省 0。
    在线判定按 _node_online（状态列 + 心跳新鲜度双条件）。
    """
    live_slots = live_slots or {}
    async with engine_pyp.connect() as conn:
        agents = (
            await conn.execute(
                select(Agent.agent_id, Agent.status, Agent.slot_n, Agent.last_heartbeat).order_by(Agent.agent_id)
            )
        ).all()
        tallies = (
            await conn.execute(
                select(
                    Request.agent_id,
                    func.count().filter(Request.state == _SUCCESS).label("ok"),
                    func.count().filter(Request.state < 0).label("fail"),
                )
                .where(Request.agent_id.is_not(None))
                .group_by(Request.agent_id)
            )
        ).all()
    by_agent = {aid: (int(ok), int(fail)) for aid, ok, fail in tallies}
    now = datetime.now(UTC)
    out: list[NodeMetric] = []
    for agent_id, status, slot_n, last_heartbeat in agents:
        ok, fail = by_agent.get(agent_id, (0, 0))
        out.append(
            NodeMetric(
                agent_id=agent_id,
                online=_node_online(status, last_heartbeat, now=now),
                slot_n=int(slot_n or 0),
                slot_used=int(live_slots.get(agent_id, 0)),
                ok=ok,
                fail=fail,
                success_rate=_rate(ok, ok + fail),
            )
        )
    return out


async def source_health(engine_pyp: AsyncEngine, *, since: datetime | None = None) -> list[SourceHealth]:
    """每数据源健康度：访问/冷却状态 + 成败率 + 数据质量 + 错误码分布。

    以 sources 为左表，因此刚创建、尚无请求的源也会出现在运维面板。since 只约束请求样本，
    不会把无样本数据源从结果中移除。
    """
    request_join = Request.batch_id == Batch.id
    if since:
        request_join = and_(request_join, Request.created_at >= since)
    health_stmt = (
        select(
            Source.uuid,
            Source.name,
            Source.access_confirmed_at,
            Source.paused_at,
            Source.pause_reason,
            Source.cooldown_until,
            Source.cooldown_reason,
            Source.rate_limit,
            Source.last_status_code,
            Source.consecutive_failures,
            Source.last_success_at,
            Source.last_failure_at,
            func.count().filter(Request.state == _SUCCESS).label("ok"),
            func.count().filter(Request.state < 0).label("fail"),
            func.coalesce(func.sum(Request.count_ok), 0).label("q_ok"),
            func.coalesce(func.sum(Request.count_fail), 0).label("q_fail"),
            func.coalesce(func.sum(Request.count_blank), 0).label("q_blank"),
        )
        .select_from(Source.__table__)
        .outerjoin(Task.__table__, Task.source_id == Source.id)
        .outerjoin(Batch.__table__, Batch.task_id == Task.id)
        .outerjoin(Request.__table__, request_join)
        .group_by(
            Source.uuid,
            Source.name,
            Source.access_confirmed_at,
            Source.paused_at,
            Source.pause_reason,
            Source.cooldown_until,
            Source.cooldown_reason,
            Source.rate_limit,
            Source.last_status_code,
            Source.consecutive_failures,
            Source.last_success_at,
            Source.last_failure_at,
        )
        .order_by(Source.uuid)
    )
    err_stmt = (
        select(Source.uuid, Request.state, func.count())
        .select_from(Request.__table__)
        .join(Batch.__table__, Request.batch_id == Batch.id)
        .join(Task.__table__, Batch.task_id == Task.id)
        .join(Source.__table__, Task.source_id == Source.id)
        .where(Request.state < 0)
        .group_by(Source.uuid, Request.state)
    )
    if since:
        err_stmt = err_stmt.where(Request.created_at >= since)
    async with engine_pyp.connect() as conn:
        rows = (await conn.execute(health_stmt)).all()  # 成败 + 质量计数
        err_rows = (await conn.execute(err_stmt)).all()  # 错误码分布（仅失败态）
    by_error: dict[str, dict[str, int]] = {}
    for uuid, state, n in err_rows:
        by_error.setdefault(uuid, {})[label(int(state))] = int(n)
    out: list[SourceHealth] = []
    now = datetime.now(UTC)
    for (
        uuid,
        name,
        confirmed_at,
        paused_at,
        pause_reason,
        cooldown_until,
        cooldown_reason,
        rate_limit,
        last_status_code,
        consecutive_failures,
        last_success_at,
        last_failure_at,
        ok,
        fail,
        q_ok,
        q_fail,
        q_blank,
    ) in rows:
        ok, fail = int(ok), int(fail)
        if paused_at is not None:
            access_state = "paused"
        elif confirmed_at is None:
            access_state = "review"
        elif cooldown_until is not None and cooldown_until > now:
            access_state = "cooling"
        else:
            access_state = "active"
        out.append(
            SourceHealth(
                source=uuid,
                name=name,
                access_state=access_state,
                pause_reason=pause_reason,
                cooldown_until=cooldown_until,
                cooldown_reason=cooldown_reason,
                rate_limit=float(rate_limit or 0),
                last_status_code=last_status_code,
                consecutive_failures=int(consecutive_failures or 0),
                last_success_at=last_success_at,
                last_failure_at=last_failure_at,
                total=ok + fail,
                ok=ok,
                fail=fail,
                success_rate=_rate(ok, ok + fail),
                quality=_quality(int(q_ok), int(q_fail), int(q_blank)),
                by_error=by_error.get(uuid, {}),
            )
        )
    return out


async def system_overview(engine_pyp: AsyncEngine, *, since: datetime | None = None) -> SystemOverview:
    """一屏总览：节点在线数 + 排队深度 + 整体请求成败 + 整体数据质量。"""
    tally_stmt = select(
        func.count().filter(Request.state == _SUCCESS).label("ok"),
        func.count().filter(Request.state < 0).label("fail"),
        func.coalesce(func.sum(Request.count_ok), 0).label("q_ok"),
        func.coalesce(func.sum(Request.count_fail), 0).label("q_fail"),
        func.coalesce(func.sum(Request.count_blank), 0).label("q_blank"),
    )
    if since:
        tally_stmt = tally_stmt.where(Request.created_at >= since)
    async with engine_pyp.connect() as conn:
        nodes_total = int((await conn.execute(select(func.count()).select_from(Agent.__table__))).scalar() or 0)
        # 与 node_metrics 同口径：状态列 + 心跳新鲜度双条件（防僵尸在线，见 _node_online）
        heartbeat_floor = datetime.now(UTC) - timedelta(seconds=HEARTBEAT_FRESH_S)
        nodes_online = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(Agent.__table__)
                    .where(Agent.status == "online", Agent.last_heartbeat >= heartbeat_floor)
                )
            ).scalar()
            or 0
        )
        queue_depth = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(Request.__table__)
                    .join(Batch.__table__, Request.batch_id == Batch.id)
                    .where(Request.state == int(RequestState.QUEUED), Batch.status == "running")
                )
            ).scalar()
            or 0
        )
        row = (await conn.execute(tally_stmt)).one()
    ok, fail, q_ok, q_fail, q_blank = (int(x) for x in row)
    return SystemOverview(
        nodes_online=nodes_online,
        nodes_total=nodes_total,
        queue_depth=queue_depth,
        requests_total=ok + fail,
        ok=ok,
        fail=fail,
        success_rate=_rate(ok, ok + fail),
        quality=_quality(q_ok, q_fail, q_blank),
    )


__all__ = ["node_metrics", "source_health", "system_overview"]
