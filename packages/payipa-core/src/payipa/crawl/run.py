"""批次/请求创建 + 结果分流入库（M1 单源端到端最小实现）。

跨库写入一致性（无分布式事务）：**先写数据（data_center，指纹幂等）→ 再置状态（pyp 的 requests/batch）**，
顺序保证「状态=成功 ⟹ 数据已落」。规模内不引入分布式事务/重型对账（SDD §4.4）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from jianbing_utils import crypto
from payipa_contracts import Channel, ErrorCode, Priority, RequestState, ResultBatch, RulePack, RulePointer, TaskSpec
from sqlalchemy import Table, case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from payipa.crawl.ingest import Ingestor, build_data_table, create_data_table
from payipa.db.pyp import Agent, Batch, Request, Rule, Schedule, Source, Task


def url_fingerprint(url: str) -> str:
    """URL 去重指纹：最小规范化（去 fragment + 去首尾空白）后 sha256。

    完整规范化（查询参数排序/百分号归一/黑白名单）由 jianbing_utils 自研模块承接（决策：URL 规范化自研），
    此处先用最小实现保证批内同 URL 去重正确。
    """
    normalized = url.split("#", 1)[0].strip()
    return crypto.sha256(normalized)


async def setup_source(
    engine_pyp: AsyncEngine,
    uuid: str,
    name: str = "M1 source",
    *,
    seed_urls: Sequence[str] | None = None,
) -> tuple[int, int]:
    """确保 source + 一个 task 存在（幂等）；返回 (source_id, task_id)。

    seed_urls 存档进 task.params（最近一次为准），供 cron/重跑无需重新提交种子（07 定时触发）。
    """
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(Source.__table__)
            .values(uuid=uuid, name=name, connector_type="web")
            .on_conflict_do_nothing(index_elements=["uuid"])
        )
        source_id = (await conn.execute(select(Source.id).where(Source.uuid == uuid))).scalar_one()
        task_id = (await conn.execute(select(Task.id).where(Task.source_id == source_id).limit(1))).scalar()
        params = {"seed_urls": list(seed_urls)} if seed_urls else None
        if task_id is None:
            task_id = (
                await conn.execute(
                    pg_insert(Task.__table__)
                    .values(source_id=source_id, trigger_type="manual", params=params or {})
                    .returning(Task.id)
                )
            ).scalar_one()
        elif params:
            await conn.execute(update(Task.__table__).where(Task.id == task_id).values(params=params))
    return source_id, task_id


async def resolve_ingest_context(engine_pyp: AsyncEngine, req_id: int) -> tuple[str, list[str], list[str]]:
    """由 req_id 反解入库上下文：(source_uuid, fingerprint_keys, indexed_fields)。"""
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Source.uuid, Rule.spec)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .join(Rule.__table__, Rule.content_hash == Request.rule_hash)
                .where(Request.id == req_id)
            )
        ).first()
    if row is None:
        raise LookupError(f"无法反解 req_id={req_id} 的入库上下文")
    uuid, spec = row
    pack = RulePack.model_validate(spec)
    indexed = [f.name for f in pack.fields if f.index]
    return uuid, list(pack.fingerprint), indexed


async def ensure_data_table(engine_dc: AsyncEngine, source_uuid: str, indexed_fields: Sequence[str] = ()) -> Table:
    """建源时程序化建 data_{uuid} 表（幂等）。"""
    table = build_data_table(source_uuid, indexed_fields)
    await create_data_table(engine_dc, table)
    return table


async def create_batch_with_requests(
    engine_pyp: AsyncEngine,
    *,
    task_id: int,
    source_uuid: str,
    targets: Sequence[str],
    rule_ptr: RulePointer,
    channel: Channel = Channel.PROD,
) -> tuple[int, list[TaskSpec]]:
    """建一个批次 + 每个 target 一条 request；返回 (batch_id, [TaskSpec])。"""
    specs: list[TaskSpec] = []
    async with engine_pyp.begin() as conn:
        batch_id = (
            await conn.execute(
                pg_insert(Batch.__table__)
                .values(task_id=task_id, channel=channel.value, status="running", started_at=func.now(), stats={})
                .returning(Batch.id)
            )
        ).scalar_one()
        for target in targets:
            req_id = (
                await conn.execute(
                    pg_insert(Request.__table__)
                    .values(
                        batch_id=batch_id,
                        target=target,
                        rule_hash=rule_ptr.content_hash,
                        rule_version=rule_ptr.version,
                        state=int(RequestState.QUEUED),
                        depth=0,
                        url_hash=url_fingerprint(target),
                    )
                    .returning(Request.id)
                )
            ).scalar_one()
            specs.append(
                TaskSpec(
                    task_id=str(task_id),
                    req_id=str(req_id),
                    batch_id=str(batch_id),
                    source=source_uuid,
                    target=target,
                    rule_ptr=rule_ptr,
                    channel=channel,
                )
            )
    return batch_id, specs


async def handle_result(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    table: Table,
    result: ResultBatch,
    *,
    fingerprint_keys: Sequence[str] = (),
) -> int:
    """收到 ResultBatch：先入库 data_center（指纹幂等）→ 再置 request 成功。返回入库行数。"""
    written = await Ingestor(engine_dc).upsert(
        table, result.items, batch_id=int(result.batch_id), fingerprint_keys=fingerprint_keys
    )
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == int(result.req_id))
            .values(state=int(RequestState.SUCCESS), lease_until=None)  # 完成即释放租约，免遭 reaper 回收
        )
    return written


async def set_request_state(engine_pyp: AsyncEngine, req_id: int, state: int) -> None:
    """置请求状态（正=正常态、负=错误码）。失败/取消回报走此。终态一律释放租约。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(state=state, error_code=state if state < 0 else None, lease_until=None)
        )


async def finalize_batch_if_done(engine_pyp: AsyncEngine, batch_id: int) -> bool:
    """无未完成 request（state 仍为排队/分派/运行）时把 running 批次标 done。返回是否本次完成收尾。

    仅对 status='running' 的批次生效——已取消/已收尾的批次不被翻回 done。
    """
    pending_states = (int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING))
    async with engine_pyp.begin() as conn:
        pending = (
            await conn.execute(
                select(func.count())
                .select_from(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state.in_(pending_states))
            )
        ).scalar()
        if pending:
            return False
        res = await conn.execute(
            update(Batch.__table__)
            .where(Batch.id == batch_id, Batch.status == "running")
            .values(status="done", finished_at=func.now())
        )
    return bool(res.rowcount)


async def batch_trigger_context(engine_pyp: AsyncEngine, batch_id: int) -> dict | None:
    """批次收尾自动触发所需上下文：所属任务的 params（含通知/推送绑定）+ 批次状态 + 成功计数。

    返回 ``{task_id, status, params, ok, total}``；批次不存在返回 None。params 里约定键（可选）：
    ``notify_bot_id``（收尾通知机器人）/ ``push_component_id`` + ``product_code``（链路自动推送）。
    """
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Batch.task_id, Batch.status, Task.params)
                .join(Task, Task.id == Batch.task_id)
                .where(Batch.id == batch_id)
            )
        ).first()
        if row is None:
            return None
        total = (
            await conn.execute(select(func.count()).select_from(Request.__table__).where(Request.batch_id == batch_id))
        ).scalar() or 0
        ok = (
            await conn.execute(
                select(func.count())
                .select_from(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state == int(RequestState.SUCCESS))
            )
        ).scalar() or 0
    return {
        "task_id": int(row.task_id),
        "status": row.status,
        "params": row.params or {},
        "ok": int(ok),
        "total": int(total),
    }


# ── M2 节点注册表（agents 表落库；hub 是运行态视图，此为权威）────────────────────
async def register_agent(
    engine_pyp: AsyncEngine,
    agent_id: str,
    *,
    hostname: str,
    slot_n: int,
    capabilities: dict,
    node_token_hash: str,
) -> tuple[int, str | None]:
    """注册/重连时 upsert agents 行（status=online、刷新 last_heartbeat/能力/槽位/凭证 hash）。

    返回 (weight, group_name)——由管理员在库中预置，回灌 hub 用于加权/分组派发；新节点默认 weight=1。
    """
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(Agent.__table__)
            .values(
                agent_id=agent_id,
                hostname=hostname,
                slot_n=slot_n,
                capabilities=capabilities,
                status="online",
                node_token_hash=node_token_hash,
                last_heartbeat=func.now(),
            )
            .on_conflict_do_update(
                index_elements=["agent_id"],
                set_={
                    "hostname": hostname,
                    "slot_n": slot_n,
                    "capabilities": capabilities,
                    "status": "online",
                    "node_token_hash": node_token_hash,
                    "last_heartbeat": func.now(),
                },
            )
        )
        row = (await conn.execute(select(Agent.weight, Agent.group_name).where(Agent.agent_id == agent_id))).first()
    return (row[0] if row else 1), (row[1] if row else None)


async def source_rate_limits(engine_pyp: AsyncEngine) -> dict[str, int]:
    """有 running 批次的各源的 rate_limit（req/s）：{source_uuid: rate_limit}，供派发环限流。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Source.uuid, Source.rate_limit)
                .select_from(Source.__table__)
                .join(Task.__table__, Task.source_id == Source.id)
                .join(Batch.__table__, Batch.task_id == Task.id)
                .where(Batch.status == "running")
                .distinct()
            )
        ).all()
    return {u: int(r) for u, r in rows}


async def source_of_request(engine_pyp: AsyncEngine, req_id: int) -> str | None:
    """由 req_id 反解数据源短码（供 AIMD 封禁降频定位源）。"""
    async with engine_pyp.connect() as conn:
        return (
            await conn.execute(
                select(Source.uuid)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == req_id)
            )
        ).scalar()


async def touch_agent(engine_pyp: AsyncEngine, agent_id: str) -> None:
    """心跳落库：刷新 last_heartbeat（供后续 liveness reaper/监控）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Agent.__table__).where(Agent.agent_id == agent_id).values(last_heartbeat=func.now()))


async def set_agent_offline(engine_pyp: AsyncEngine, agent_id: str) -> None:
    """断连落库：status=offline（不删行，保留历史/权重/分组配置）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Agent.__table__).where(Agent.agent_id == agent_id).values(status="offline"))


# ── M2 派发/回收（PG 权威；由 server 的后台派发环调用，core 不启循环）─────────────
_INFLIGHT = (int(RequestState.ASSIGNED), int(RequestState.RUNNING))  # 「在途」= 已占用未终结


_PRIORITY_RANK = case((Task.priority == "high", 0), (Task.priority == "mid", 1), else_=2)  # 高优先插队


async def claim_queued_for_dispatch(engine_pyp: AsyncEngine, *, limit: int = 16) -> list[TaskSpec]:
    """只读扫描 running 批次下 state=QUEUED 的请求，组装成可下发的 TaskSpec。

    **不改状态**——真正占用由 :func:`mark_assigned` 的乐观锁完成，避免读到即算派发。
    排序=三元 score（07 定案）：(优先级档, 深度升序=BFS, 入队序)。
    """
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    Request.id,
                    Request.target,
                    Request.rule_hash,
                    Request.rule_version,
                    Batch.id,
                    Batch.channel,
                    Task.id,
                    Task.priority,
                    Task.group_name,
                    Source.uuid,
                    Rule.id,
                )
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .join(Rule.__table__, Rule.content_hash == Request.rule_hash)
                .where(Request.state == int(RequestState.QUEUED), Batch.status == "running")
                .order_by(_PRIORITY_RANK, Request.depth, Request.created_at, Request.id)
                .limit(limit)
            )
        ).all()
    specs: list[TaskSpec] = []
    for req_id, target, rule_hash, rule_version, batch_id, channel, task_id, priority, group, source_uuid, rid in rows:
        specs.append(
            TaskSpec(
                task_id=str(task_id),
                req_id=str(req_id),
                batch_id=str(batch_id),
                source=source_uuid,
                target=target,
                rule_ptr=RulePointer(rule_id=str(rid), version=int(rule_version or 0), content_hash=rule_hash),
                channel=Channel(channel),
                priority=Priority(priority or "mid"),
                group=group,
            )
        )
    return specs


async def mark_assigned(engine_pyp: AsyncEngine, req_id: int, agent_id: str, lease_until: datetime) -> int:
    """乐观占用：仅当仍为 QUEUED 才置 ASSIGNED 并写 agent_id/lease_until。返回受影响行数（1=占用成功）。

    调用方必须先检查返回 1 再下发 TaskAssign，否则可能重复派发同一请求。
    """
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id, Request.state == int(RequestState.QUEUED))
            .values(state=int(RequestState.ASSIGNED), agent_id=agent_id, lease_until=lease_until)
        )
    return res.rowcount


async def requeue_request(engine_pyp: AsyncEngine, req_id: int) -> int:
    """把一条已 ASSIGNED 但下发失败（WS 发送异常）的请求退回 QUEUED；未真正执行，不计 attempt。"""
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id, Request.state == int(RequestState.ASSIGNED))
            .values(state=int(RequestState.QUEUED), lease_until=None, agent_id=None)
        )
    return res.rowcount


async def _requeue_or_giveup(conn: AsyncConnection, base_where: list, max_attempt: int) -> int:
    """符合 base_where 的在途请求：未达 max_attempt → 回 QUEUED(attempt+1)；已达 → 定格 NODE_LOST(-6)。

    仅对 running 批次重排；批次已取消/收尾的在途请求直接置 CANCELED（不再回队、也不计失联）。
    """
    running = select(Batch.id).where(Batch.status == "running").scalar_subquery()
    canceled = await conn.execute(
        update(Request.__table__)
        .where(*base_where, Request.state.in_(_INFLIGHT), Request.batch_id.not_in(running))
        .values(state=int(RequestState.CANCELED), lease_until=None)
    )
    give_up = await conn.execute(
        update(Request.__table__)
        .where(
            *base_where, Request.state.in_(_INFLIGHT), Request.batch_id.in_(running), Request.attempt + 1 >= max_attempt
        )
        .values(state=int(ErrorCode.NODE_LOST), error_code=int(ErrorCode.NODE_LOST), lease_until=None)
    )
    requeue = await conn.execute(
        update(Request.__table__)
        .where(
            *base_where, Request.state.in_(_INFLIGHT), Request.batch_id.in_(running), Request.attempt + 1 < max_attempt
        )
        .values(state=int(RequestState.QUEUED), attempt=Request.attempt + 1, lease_until=None, agent_id=None)
    )
    return (canceled.rowcount or 0) + (give_up.rowcount or 0) + (requeue.rowcount or 0)


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


async def enqueue_discovered(engine_pyp: AsyncEngine, parent_req_id: int, urls: Sequence[str]) -> int:
    """多波爬行：把父请求本页发现的链接并入**同一批次**入队。

    URL 指纹批内去重（唯一索引 (batch_id, url_hash) + ON CONFLICT DO NOTHING）；depth=父+1；
    仅当 depth ≤ rule.crawl.max_depth 才入队（无 crawl 规则 ⇒ max_depth=0 ⇒ 不跟进，退化为单页）。
    返回实际新入队条数。**须在批次收尾判定前调用**，新 QUEUED 落库以防跨波提前 finalize。
    """
    if not urls:
        return 0
    async with engine_pyp.begin() as conn:
        parent = (
            await conn.execute(
                select(Request.batch_id, Request.depth, Request.rule_hash, Request.rule_version).where(
                    Request.id == parent_req_id
                )
            )
        ).first()
        if parent is None:
            return 0
        batch_id, parent_depth, rule_hash, rule_version = parent
        child_depth = (parent_depth or 0) + 1
        spec = None
        if rule_hash:
            spec = (await conn.execute(select(Rule.spec).where(Rule.content_hash == rule_hash))).scalar()
        pack = RulePack.model_validate(spec) if spec else None
        max_depth = pack.crawl.max_depth if (pack and pack.crawl) else 0
        if child_depth > max_depth:
            return 0
        inserted = 0
        seen: set[str] = set()
        for url in urls:
            uh = url_fingerprint(url)
            if uh in seen:  # 先去同页内重复，减少无谓 INSERT
                continue
            seen.add(uh)
            res = await conn.execute(
                pg_insert(Request.__table__)
                .values(
                    batch_id=batch_id,
                    target=url,
                    rule_hash=rule_hash,
                    rule_version=rule_version,
                    state=int(RequestState.QUEUED),
                    depth=child_depth,
                    url_hash=uh,
                )
                .on_conflict_do_nothing(index_elements=["batch_id", "url_hash"])
            )
            inserted += res.rowcount or 0
    return inserted


# ── M2 监控聚合（供 /api/monitor 端点；仅读）──────────────────────────────────
async def batch_progress(engine_pyp: AsyncEngine, batch_id: int) -> dict:
    """按 state 实时聚合批次进度：{total, ok, fail, running, pct}。

    ok=SUCCESS(3)；fail=state<0；running=未终结(QUEUED/ASSIGNED/RUNNING)；pct=已终结/总数×100。
    CANCELED(4) 计入 total 且算作已终结（不入 ok/fail/running）。
    """
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Request.state, func.count()).where(Request.batch_id == batch_id).group_by(Request.state)
            )
        ).all()
    total = ok = fail = running = 0
    for state, n in rows:
        total += n
        if state == int(RequestState.SUCCESS):
            ok += n
        elif state < 0:
            fail += n
        elif state in _INFLIGHT or state == int(RequestState.QUEUED):
            running += n
    pct = round((total - running) / total * 100, 1) if total else 0.0
    return {"total": total, "ok": ok, "fail": fail, "running": running, "pct": pct}


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


# ── M2 取消 + 定时触发 ────────────────────────────────────────────────────────
async def cancel_batch(engine_pyp: AsyncEngine, batch_id: int) -> tuple[list[str], list[int]]:
    """取消一个批次：清空其 QUEUED（直接置 CANCELED），把在途 ASSIGNED/RUNNING 标记待取消（返回其 req_id
    供主控向 agent 发 Cancel 帧），批次置 canceling。返回 (在途 req_id 列表, 涉及的 agent 无关)。

    返回 (inflight_req_ids, queued_ids)：inflight 需通知 agent 优雅收尾，queued 已就地取消。
    """
    async with engine_pyp.begin() as conn:
        queued = list(
            (
                await conn.execute(
                    select(Request.id).where(Request.batch_id == batch_id, Request.state == int(RequestState.QUEUED))
                )
            )
            .scalars()
            .all()
        )
        if queued:
            await conn.execute(
                update(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state == int(RequestState.QUEUED))
                .values(state=int(RequestState.CANCELED), lease_until=None)
            )
        inflight = list(
            (await conn.execute(select(Request.id).where(Request.batch_id == batch_id, Request.state.in_(_INFLIGHT))))
            .scalars()
            .all()
        )
        await conn.execute(
            update(Batch.__table__)
            .where(Batch.id == batch_id, Batch.status == "running")
            .values(status="canceling" if inflight else "canceled", finished_at=None if inflight else func.now())
        )
    return [str(r) for r in inflight], queued


async def sweep_canceling_batches(engine_pyp: AsyncEngine) -> int:
    """把已无未完成请求的 canceling 批次收口为 canceled（在途 agent 回报 CANCELED 后）。返回收口数。"""
    pending = (int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING))
    async with engine_pyp.begin() as conn:
        stuck = select(Request.batch_id).where(Request.state.in_(pending)).distinct().scalar_subquery()
        res = await conn.execute(
            update(Batch.__table__)
            .where(Batch.status == "canceling", Batch.id.not_in(stuck))
            .values(status="canceled", finished_at=func.now())
        )
    return res.rowcount or 0


async def due_schedules(engine_pyp: AsyncEngine) -> list[tuple[int, int, str, str, list[str]]]:
    """返回到点（next_run_at ≤ now 或未初始化）且 enabled 的调度。

    每项 = (schedule_id, task_id, cron_expr, source_uuid, seed_urls)。seed_urls 取自 task.params；触发时据此
    建新批次（复用建源存档的种子）。next_run_at 的推进由调用方在成功建批后调用 :func:`advance_schedule` 完成。
    """
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Schedule.id, Task.id, Schedule.cron_expr, Source.uuid, Task.params)
                .select_from(Schedule.__table__)
                .join(Task.__table__, Schedule.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(
                    Schedule.enabled.is_(True),
                    (Schedule.next_run_at.is_(None)) | (Schedule.next_run_at <= func.now()),
                )
            )
        ).all()
    out: list[tuple[int, int, str, str, list[str]]] = []
    for sched_id, task_id, cron_expr, source_uuid, params in rows:
        seeds = list((params or {}).get("seed_urls") or [])
        out.append((sched_id, task_id, cron_expr, source_uuid, seeds))
    return out


async def advance_schedule(engine_pyp: AsyncEngine, schedule_id: int, next_run_at: datetime) -> None:
    """把调度的下次运行时间推进到 next_run_at（由调用方用 cron 表达式算出）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Schedule.__table__).where(Schedule.id == schedule_id).values(next_run_at=next_run_at))


async def create_batch_for_task(
    engine_pyp: AsyncEngine,
    *,
    task_id: int,
    source_uuid: str,
    seed_urls: Sequence[str],
    channel: Channel = Channel.PROD,
) -> tuple[int, list[TaskSpec]]:
    """按已存在的 task + 其数据源当前 active 规则建一个新批次（供 cron/API 重跑，无需重新提交规则）。"""
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(Rule.id, Rule.version, Rule.content_hash)
                .join(Task.__table__, Task.source_id == Rule.source_id)
                .where(Task.id == task_id)
                .order_by(Rule.version.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        raise LookupError(f"task {task_id} 无可用规则，无法建批次")
    ptr = RulePointer(rule_id=str(row[0]), version=row[1], content_hash=row[2])
    return await create_batch_with_requests(
        engine_pyp, task_id=task_id, source_uuid=source_uuid, targets=seed_urls, rule_ptr=ptr, channel=channel
    )
