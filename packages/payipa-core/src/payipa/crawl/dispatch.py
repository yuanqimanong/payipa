"""持久化派发队列：能力过滤、公平扫描、ACK 租约与回收。"""

from __future__ import annotations

from datetime import datetime

from payipa_contracts import (
    Channel,
    EngineHint,
    ErrorCode,
    Priority,
    RequestState,
    RulePointer,
    TaskSpec,
)
from sqlalchemy import case, func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from payipa.crawl._policy import _INFLIGHT, _allowed_domains
from payipa.db.pyp import Batch, Request, Rule, Source, Task

_PRIORITY_RANK = case((Task.priority == "high", 0), (Task.priority == "mid", 1), else_=2)  # 高优先插队


_CRAWL_STRATEGY = Rule.spec["crawl"]["strategy"].astext


_DEPTH_RANK = case((_CRAWL_STRATEGY == "dfs", -Request.depth), else_=Request.depth)


def _authorized_running_batch_ids():
    return (
        select(Batch.id)
        .join(Task.__table__, Batch.task_id == Task.id)
        .join(Source.__table__, Task.source_id == Source.id)
        .where(
            Batch.status == "running",
            Source.access_confirmed_at.is_not(None),
            Source.paused_at.is_(None),
        )
    )


def _active_running_batch_ids():
    return (
        select(Batch.id)
        .join(Task.__table__, Batch.task_id == Task.id)
        .join(Source.__table__, Task.source_id == Source.id)
        .where(
            Batch.status == "running",
            Source.access_confirmed_at.is_not(None),
            Source.paused_at.is_(None),
            (Source.cooldown_until.is_(None)) | (Source.cooldown_until <= func.now()),
        )
    )


def _cap_filter(caps: dict[str | None, set[str]]):
    """能力过滤条件：只捞「当前有空闲同组节点且具备目标引擎」的请求（P0-11 防队头饥饿）。

    caps 形如 {None: 全部空闲节点引擎并集, 组名: 该组空闲节点引擎并集}。
    未分组请求可派任意空闲节点（对应 None 键）；分组请求只看本组。
    """
    grp = func.coalesce(Task.group_name, Source.agent_group)
    raw = func.coalesce(Task.params.op("->>")("engine_hint"), EngineHint.HTTP.value)
    # 未知 engine_hint 与 Python 侧解析同语义：回退 http
    eng = case((raw.in_([e.value for e in EngineHint]), raw), else_=EngineHint.HTTP.value)
    conds = []
    if caps.get(None):
        conds.append(grp.is_(None) & eng.in_(sorted(caps[None])))
    pairs = [(g, e) for g, es in caps.items() if g is not None for e in sorted(es)]
    if pairs:
        conds.append(tuple_(grp, eng).in_(pairs))
    return or_(*conds) if conds else None


async def claim_queued_for_dispatch(
    engine_pyp: AsyncEngine, *, limit: int = 16, caps: dict[str | None, set[str]] | None = None
) -> list[TaskSpec]:
    """只读扫描 running 批次下 state=QUEUED 的请求，组装成可下发的 TaskSpec。

    **不改状态**——真正占用由 :func:`mark_assigned` 的乐观锁完成，避免读到即算派发。
    排序：先按源轮转（row_number 分源取第 N 条，单源积压不能霸占窗口），
    同轮内仍按三元 score（07 定案）：(优先级档, BFS 深度升序/DFS 深度降序, 入队序)。
    caps 非 None 时按在线能力过滤（见 :func:`_cap_filter`），队头缺能力的请求不占窗口。
    """
    if caps is not None and not caps:
        return []  # 无空闲节点，无需扫描
    rr = func.row_number().over(  # 每源内的名次：跨源轮转用
        partition_by=Source.uuid,
        order_by=(_PRIORITY_RANK, _DEPTH_RANK, Request.created_at, Request.id),
    )
    async with engine_pyp.connect() as conn:
        stmt = (
            select(
                Request.id,
                Request.target,
                Request.attempt,
                Rule.content_hash,
                Rule.version,
                Batch.id,
                Batch.channel,
                Task.id,
                Task.priority,
                Task.group_name,
                Task.params,
                Source.uuid,
                Source.timeout,
                Source.agent_group,
                Source.raw_archive,
                Rule.id,
            )
            .select_from(Request.__table__)
            .join(Batch.__table__, Request.batch_id == Batch.id)
            .join(Task.__table__, Batch.task_id == Task.id)
            .join(Source.__table__, Task.source_id == Source.id)
            .join(Rule.__table__, Rule.id == Request.rule_id)
            .where(
                Request.state == int(RequestState.QUEUED),
                (Request.not_before.is_(None)) | (Request.not_before <= func.now()),
                Batch.status == "running",
                Source.access_confirmed_at.is_not(None),
                Source.paused_at.is_(None),
                (Source.cooldown_until.is_(None)) | (Source.cooldown_until <= func.now()),
            )
            .order_by(rr, _PRIORITY_RANK, _DEPTH_RANK, Request.created_at, Request.id)
            .limit(limit)
        )
        if caps is not None:
            cond = _cap_filter(caps)
            if cond is None:
                return []
            stmt = stmt.where(cond)
        rows = (await conn.execute(stmt)).all()
    specs: list[TaskSpec] = []
    for (
        req_id,
        target,
        attempt,
        rule_hash,
        rule_version,
        batch_id,
        channel,
        task_id,
        priority,
        task_group,
        params,
        source_uuid,
        timeout_s,
        source_group,
        raw_archive,
        rid,
    ) in rows:
        params = params or {}
        try:
            engine_hint = EngineHint(params.get("engine_hint", EngineHint.HTTP))
        except TypeError, ValueError:
            engine_hint = EngineHint.HTTP
        specs.append(
            TaskSpec(
                task_id=str(task_id),
                req_id=str(req_id),
                batch_id=str(batch_id),
                source=source_uuid,
                target=target,
                allowed_domains=_allowed_domains(params, target),
                rule_ptr=RulePointer(rule_id=str(rid), version=int(rule_version or 0), content_hash=rule_hash),
                channel=Channel(channel),
                priority=Priority(priority or "mid"),
                timeout_s=max(1, int(timeout_s or 30)),
                attempt=int(attempt or 0),
                engine_hint=engine_hint,
                group=task_group or source_group,
                archive_raw=bool(raw_archive) and Channel(channel) is Channel.PROD,
            )
        )
    return specs


def _db_lease(lease_s: int):
    """租约到期时间用**数据库时钟**算（now()+interval）：写入与回收同源，应用侧时钟漂移不影响租约。"""
    return func.now() + text(f"interval '{int(lease_s)} seconds'")


async def mark_assigned(
    engine_pyp: AsyncEngine,
    req_id: int,
    agent_id: str,
    lease_until: datetime | None = None,
    *,
    attempt: int | None = None,
    lease_s: int | None = None,
) -> int:
    """乐观占用：仅当仍为 QUEUED 才置 ASSIGNED 并写 agent_id/租约。返回受影响行数（1=占用成功）。

    调用方必须先检查返回 1 再下发 TaskAssign，否则可能重复派发同一请求。
    传 attempt 时代次也须吻合（P0-10：claim 到 CAS 之间被重试推进的请求会干净地抢占失败）。
    租约优先用 lease_s（DB 时钟，推荐）；lease_until 为兼容旧调用方的应用侧时刻。
    """
    conds = [
        Request.id == req_id,
        Request.state == int(RequestState.QUEUED),
        (Request.not_before.is_(None)) | (Request.not_before <= func.now()),
        Request.batch_id.in_(_active_running_batch_ids()),
    ]
    if attempt is not None:
        conds.append(Request.attempt == attempt)
    lease = _db_lease(lease_s) if lease_s is not None else lease_until
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Request.__table__)
            .where(*conds)
            .values(state=int(RequestState.ASSIGNED), agent_id=agent_id, lease_until=lease)
        )
    return res.rowcount


async def mark_running(engine_pyp: AsyncEngine, req_id: int, agent_id: str, attempt: int, *, lease_s: int) -> int:
    """任务 ACK（P0-10）：ASSIGNED→RUNNING（校验归属与代次），并把 ACK 短租展成执行租约。

    返回受影响行数；0=迟到/越权 ack（已被回收重派），调用方记日志即可。
    """
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Request.__table__)
            .where(
                Request.id == req_id,
                Request.state == int(RequestState.ASSIGNED),
                Request.agent_id == agent_id,
                Request.attempt == attempt,
            )
            .values(state=int(RequestState.RUNNING), lease_until=_db_lease(lease_s))
        )
    return res.rowcount


async def requeue_request(engine_pyp: AsyncEngine, req_id: int) -> int:
    """把一条已 ASSIGNED 但下发失败（WS 发送异常）的请求退回 QUEUED；未真正执行，不计 attempt。"""
    async with engine_pyp.begin() as conn:
        running = select(Batch.id).where(Batch.status == "running")
        authorized = _authorized_running_batch_ids()
        requeued = await conn.execute(
            update(Request.__table__)
            .where(
                Request.id == req_id,
                Request.state == int(RequestState.ASSIGNED),
                Request.batch_id.in_(authorized),
            )
            .values(state=int(RequestState.QUEUED), not_before=None, lease_until=None, agent_id=None)
        )
        canceled = await conn.execute(
            update(Request.__table__)
            .where(
                Request.id == req_id,
                Request.state == int(RequestState.ASSIGNED),
                Request.batch_id.not_in(running),
            )
            .values(state=int(RequestState.CANCELED), lease_until=None, agent_id=None)
        )
        paused = await conn.execute(
            update(Request.__table__)
            .where(
                Request.id == req_id,
                Request.state == int(RequestState.ASSIGNED),
                Request.batch_id.in_(running),
                Request.batch_id.not_in(authorized),
            )
            .values(
                state=int(ErrorCode.ACCESS_PAUSED),
                error_code=int(ErrorCode.ACCESS_PAUSED),
                lease_until=None,
                agent_id=None,
            )
        )
    return (requeued.rowcount or 0) + (canceled.rowcount or 0) + (paused.rowcount or 0)


async def _requeue_or_giveup(conn: AsyncConnection, base_where: list, max_attempt: int) -> int:
    """符合 base_where 的在途请求：未达 max_attempt → 回 QUEUED(attempt+1)；已达 → 定格 NODE_LOST(-6)。

    仅对 running 批次重排；批次已取消/收尾的在途请求直接置 CANCELED（不再回队、也不计失联）。
    """
    running = select(Batch.id).where(Batch.status == "running")
    authorized = _authorized_running_batch_ids()
    canceled = await conn.execute(
        update(Request.__table__)
        .where(*base_where, Request.state.in_(_INFLIGHT), Request.batch_id.not_in(running))
        .values(state=int(RequestState.CANCELED), lease_until=None)
    )
    access_paused = await conn.execute(
        update(Request.__table__)
        .where(
            *base_where,
            Request.state.in_(_INFLIGHT),
            Request.batch_id.in_(running),
            Request.batch_id.not_in(authorized),
        )
        .values(
            state=int(ErrorCode.ACCESS_PAUSED),
            error_code=int(ErrorCode.ACCESS_PAUSED),
            lease_until=None,
            agent_id=None,
        )
    )
    give_up = await conn.execute(
        update(Request.__table__)
        .where(
            *base_where,
            Request.state.in_(_INFLIGHT),
            Request.batch_id.in_(authorized),
            Request.attempt + 1 >= max_attempt,
        )
        .values(
            state=int(ErrorCode.NODE_LOST),
            error_code=int(ErrorCode.NODE_LOST),
            not_before=None,
            lease_until=None,
        )
    )
    requeue = await conn.execute(
        update(Request.__table__)
        .where(
            *base_where,
            Request.state.in_(_INFLIGHT),
            Request.batch_id.in_(authorized),
            Request.attempt + 1 < max_attempt,
        )
        .values(
            state=int(RequestState.QUEUED),
            attempt=Request.attempt + 1,
            not_before=None,
            lease_until=None,
            agent_id=None,
        )
    )
    return (canceled.rowcount or 0) + (access_paused.rowcount or 0) + (give_up.rowcount or 0) + (requeue.rowcount or 0)


async def requeue_expired_leases(engine_pyp: AsyncEngine, *, max_attempt: int = 3) -> int:
    """回收租约到期（agent 疑似失联/挂起）的在途请求。以 DB 时钟 func.now() 为准，避免应用/库时钟偏差。"""
    async with engine_pyp.begin() as conn:
        return await _requeue_or_giveup(
            conn, [Request.lease_until.is_not(None), Request.lease_until < func.now()], max_attempt
        )


async def requeue_agent_inflight(engine_pyp: AsyncEngine, agent_id: str, *, max_attempt: int = 3) -> int:
    """agent 断连即回收其在途请求（快速路径，不等租约超时）；由 WS 端点在 finally 中调用。"""
    async with engine_pyp.begin() as conn:
        return await _requeue_or_giveup(conn, [Request.agent_id == agent_id], max_attempt)


async def queue_depth(engine_pyp: AsyncEngine) -> dict[str, int]:
    """running 批次下 state=QUEUED 请求的排队深度，按任务优先级(high/mid/low)分桶。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Task.priority, func.count())
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .where(Request.state == int(RequestState.QUEUED), Batch.status == "running")
                .group_by(Task.priority)
            )
        ).all()
    return {(p or "mid"): int(n) for p, n in rows}
