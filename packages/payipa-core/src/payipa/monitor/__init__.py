"""monitor —— 主控侧全局聚合（09 定案：core 无 agent 模块，agent 只回报原始计数，聚合在此）。

汇总三类指标（跨 agent + 任务 + 数据质量的全局视图）：
- **节点**：agents 在线态/槽位 + 历史成败（requests 按 agent_id 分组）→ NodeMetric；
- **数据源健康**：每源请求成败率 + 数据质量（解析成功·失败·空白率）→ SourceHealth；
- **系统总览**：节点/队列/请求成败/整体质量一屏 → SystemOverview。

数据质量来自 requests 的 count_ok/fail/blank（agent ExecSummary 回填，见 crawl/run.handle_result）；
无样本（NULL/0）时质量记 None、成功率记 1.0（诚实：没有负面信号）。接口在 apps/server，页在 06（SSE 实时）。
"""

from __future__ import annotations

from datetime import datetime

from payipa_contracts import (
    NodeMetric,
    QualityMetric,
    RequestState,
    SourceHealth,
    SystemOverview,
    label,
)
from sqlalchemy import func, select
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


async def node_metrics(engine_pyp: AsyncEngine, live_slots: dict[str, int] | None = None) -> list[NodeMetric]:
    """每节点聚合：agents 表在线态/槽位 + requests 历史成败（按 agent_id）。

    live_slots：运行态已占槽（来自 AgentHub 记账，DB 无此实时值）；缺省 0。
    """
    live_slots = live_slots or {}
    async with engine_pyp.connect() as conn:
        agents = (await conn.execute(select(Agent.agent_id, Agent.status, Agent.slot_n).order_by(Agent.agent_id))).all()
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
    out: list[NodeMetric] = []
    for agent_id, status, slot_n in agents:
        ok, fail = by_agent.get(agent_id, (0, 0))
        out.append(
            NodeMetric(
                agent_id=agent_id,
                online=(status == "online"),
                slot_n=int(slot_n or 0),
                slot_used=int(live_slots.get(agent_id, 0)),
                ok=ok,
                fail=fail,
                success_rate=_rate(ok, ok + fail),
            )
        )
    return out


async def source_health(engine_pyp: AsyncEngine, *, since: datetime | None = None) -> list[SourceHealth]:
    """每数据源健康度：成败率 + 数据质量 + 错误码分布。since 限定 requests.created_at 之后（可选窗口）。"""
    health_stmt = (
        select(
            Source.uuid,
            func.count().filter(Request.state == _SUCCESS).label("ok"),
            func.count().filter(Request.state < 0).label("fail"),
            func.coalesce(func.sum(Request.count_ok), 0).label("q_ok"),
            func.coalesce(func.sum(Request.count_fail), 0).label("q_fail"),
            func.coalesce(func.sum(Request.count_blank), 0).label("q_blank"),
        )
        .select_from(Request.__table__)
        .join(Batch.__table__, Request.batch_id == Batch.id)
        .join(Task.__table__, Batch.task_id == Task.id)
        .join(Source.__table__, Task.source_id == Source.id)
        .group_by(Source.uuid)
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
        health_stmt = health_stmt.where(Request.created_at >= since)
        err_stmt = err_stmt.where(Request.created_at >= since)
    async with engine_pyp.connect() as conn:
        rows = (await conn.execute(health_stmt)).all()  # 成败 + 质量计数
        err_rows = (await conn.execute(err_stmt)).all()  # 错误码分布（仅失败态）
    by_error: dict[str, dict[str, int]] = {}
    for uuid, state, n in err_rows:
        by_error.setdefault(uuid, {})[label(int(state))] = int(n)
    out: list[SourceHealth] = []
    for uuid, ok, fail, q_ok, q_fail, q_blank in rows:
        ok, fail = int(ok), int(fail)
        out.append(
            SourceHealth(
                source=uuid,
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
        nodes_online = int(
            (
                await conn.execute(select(func.count()).select_from(Agent.__table__).where(Agent.status == "online"))
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
