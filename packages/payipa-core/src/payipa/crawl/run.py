"""批次/请求创建 + 结果分流入库（M1 单源端到端最小实现）。

跨库写入一致性（无分布式事务）：**先写数据（data_center，指纹幂等）→ 再置状态（pyp 的 requests/batch）**，
顺序保证「状态=成功 ⟹ 数据已落」。规模内不引入分布式事务/重型对账（SDD §4.4）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from jianbing_utils import crypto
from payipa_contracts import (
    Channel,
    EngineHint,
    ErrorCode,
    Priority,
    RequestState,
    ResultBatch,
    RulePack,
    RulePointer,
    TaskSpec,
)
from sqlalchemy import Table, case, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from payipa.crawl.ingest import Ingestor, build_data_table, create_data_table
from payipa.db.ident import check_code
from payipa.db.pyp import Agent, Batch, Request, Rule, Schedule, Source, Task, TaskEvent

ACCESS_BASES = frozenset({"owned", "contracted", "public_policy"})


def _validate_access_record(access_basis: str | None, access_reference: str | None) -> tuple[str, str]:
    basis = (access_basis or "").strip()
    reference = (access_reference or "").strip()
    if basis not in ACCESS_BASES:
        raise ValueError(f"access_basis 必须是 {', '.join(sorted(ACCESS_BASES))} 之一")
    if not reference:
        raise ValueError("access_reference 必须记录授权文件、合同、API 文档或公开访问政策")
    return basis, reference


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
    access_basis: str | None = None,
    access_reference: str | None = None,
    access_confirmed: bool = False,
    engine_hint: EngineHint | None = None,
    rate_limit: int | None = None,
    retry: int | None = None,
    timeout: int | None = None,
    raw_archive: bool | None = None,
) -> tuple[int, int]:
    """确保已确认访问依据的 source + 一个 task 存在；返回 (source_id, task_id)。

    seed_urls 存档进 task.params（最近一次为准），供 cron/重跑无需重新提交种子（07 定时触发）。
    新数据源必须显式确认访问依据；既有数据源一旦暂停，只能经人工复核接口恢复。
    """
    check_code(uuid)  # 短码进 data_{uuid} 分表名/对象存储 key，落库前先过统一校验（P0-13）
    if rate_limit is not None and not 1 <= rate_limit <= 1000:
        raise ValueError("rate_limit 必须在 1–1000 req/s 之间")
    if retry is not None and not 1 <= retry <= 10:
        raise ValueError("retry 必须在 1–10 次之间")
    if timeout is not None and not 5 <= timeout <= 1800:
        raise ValueError("timeout 必须在 5–1800 秒之间")
    async with engine_pyp.begin() as conn:
        source = (
            await conn.execute(
                select(Source.id, Source.access_confirmed_at, Source.paused_at).where(Source.uuid == uuid)
            )
        ).first()
        if source is None:
            if not access_confirmed:
                raise PermissionError("新数据源必须由操作者确认访问授权")
            basis, reference = _validate_access_record(access_basis, access_reference)
            source_id = (
                await conn.execute(
                    pg_insert(Source.__table__)
                    .values(
                        uuid=uuid,
                        name=name,
                        connector_type="web",
                        access_basis=basis,
                        access_reference=reference,
                        access_confirmed_at=func.now(),
                        rate_limit=rate_limit if rate_limit is not None else 10,
                        retry=retry if retry is not None else 3,
                        timeout=timeout if timeout is not None else 30,
                        raw_archive=bool(raw_archive),
                    )
                    .returning(Source.id)
                )
            ).scalar_one()
        else:
            source_id, confirmed_at, paused_at = source
            if confirmed_at is None:
                raise PermissionError("数据源尚未完成人工访问授权复核")
            if paused_at is not None:
                raise PermissionError("数据源已暂停，须完成人工复核后才能恢复")
            source_values: dict = {"name": name}
            for key, value in {
                "rate_limit": rate_limit,
                "retry": retry,
                "timeout": timeout,
                "raw_archive": raw_archive,
            }.items():
                if value is not None:
                    source_values[key] = value
            await conn.execute(update(Source.__table__).where(Source.id == source_id).values(**source_values))
        task_row = (
            await conn.execute(select(Task.id, Task.params).where(Task.source_id == source_id).limit(1))
        ).first()
        params = dict(task_row.params or {}) if task_row is not None else {}
        if seed_urls:
            params["seed_urls"] = list(seed_urls)
        if engine_hint is not None:
            params["engine_hint"] = engine_hint.value
        elif task_row is None:
            params["engine_hint"] = EngineHint.HTTP.value
        if task_row is None:
            task_id = (
                await conn.execute(
                    pg_insert(Task.__table__)
                    .values(source_id=source_id, trigger_type="manual", params=params)
                    .returning(Task.id)
                )
            ).scalar_one()
        else:
            task_id = task_row.id
            await conn.execute(update(Task.__table__).where(Task.id == task_id).values(params=params))
    return source_id, task_id


async def review_source_access(
    engine_pyp: AsyncEngine,
    uuid: str,
    *,
    access_basis: str,
    access_reference: str,
    approved: bool,
    reason: str | None = None,
) -> bool:
    """记录人工访问复核。批准会恢复调度；拒绝会保持整源暂停。"""
    basis, reference = _validate_access_record(access_basis, access_reference)
    values: dict = {
        "access_basis": basis,
        "access_reference": reference,
        "access_confirmed_at": func.now() if approved else None,
        "paused_at": None if approved else func.now(),
        "pause_reason": None if approved else (reason or "人工访问复核未通过")[:1000],
        "cooldown_until": None,
        "cooldown_reason": None,
        "consecutive_failures": 0 if approved else Source.consecutive_failures,
    }
    async with engine_pyp.begin() as conn:
        result = await conn.execute(update(Source.__table__).where(Source.uuid == uuid).values(**values))
    return bool(result.rowcount)


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
                .join(Rule.__table__, Rule.id == Request.rule_id)
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
    """为已批准且未暂停的数据源建批次；返回 ``(batch_id, TaskSpec 列表)``。"""
    specs: list[TaskSpec] = []
    async with engine_pyp.begin() as conn:
        source = (
            await conn.execute(
                select(Source.access_confirmed_at, Source.paused_at)
                .select_from(Task.__table__)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Task.id == task_id, Source.uuid == source_uuid)
            )
        ).first()
        if source is None:
            raise LookupError(f"task {task_id} 与数据源 {source_uuid!r} 不匹配")
        if source.access_confirmed_at is None:
            raise PermissionError("数据源尚未完成人工访问授权复核")
        if source.paused_at is not None:
            raise PermissionError("数据源已暂停，不能创建新批次")
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
                        rule_id=int(rule_ptr.rule_id),
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


def _is_stale(row, agent_id: str | None, attempt: int | None) -> bool:
    """迟到/越权回报判定（P0-10 fencing）：终态、归属不符或代次不符的回报一律视为 stale。"""
    if row is None:
        return True
    if int(row.state) not in (int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING)):
        return True  # 已终结（成功/取消/失败）：迟到结果不得覆盖
    if agent_id is not None and row.agent_id != agent_id:
        return True  # 回报者不是当前持有者（重派/回收后 agent_id 已换或清空）
    return attempt is not None and int(row.attempt or 0) != attempt


async def fence_ok(engine_pyp: AsyncEngine, req_id: int, agent_id: str | None, attempt: int | None) -> bool:
    """只读 fencing 预检（供 ws 在续爬入队前调用）；stale 时记 task_event 并返回 False。

    权威校验仍在 :func:`handle_result` 事务内再做一次（预检到写入之间的窄窗口竞态由其兜底）。
    """
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Request.state, Request.agent_id, Request.attempt, Request.batch_id).where(Request.id == req_id)
            )
        ).first()
        if not _is_stale(row, agent_id, attempt):
            return True
        if row is not None:
            await conn.execute(
                TaskEvent.__table__.insert().values(
                    batch_id=row.batch_id,
                    type="request.stale_result",
                    payload={"req_id": req_id, "agent_id": agent_id, "attempt": attempt, "state": int(row.state)},
                )
            )
    return False


async def handle_result(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    table: Table,
    result: ResultBatch,
    *,
    fingerprint_keys: Sequence[str] = (),
    agent_id: str | None = None,
) -> int:
    """收到 ResultBatch：fencing 校验 → 入库 data_center（指纹幂等）→ 置 request 成功。返回入库行数。

    fencing（P0-10）：在 pyp 事务内先锁行校验（状态在途 + agent 归属 + attempt 代次），迟到/重派后的
    旧结果既不写数据也不改状态，返回 0。锁行在 data_center 写之前取得，保持「状态=成功 ⟹ 数据已落」。
    同时把 agent 回报的执行摘要计数落到 request 行，供 core.monitor 聚合数据质量与时延（M5）。
    """
    s = result.summary
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Request.state, Request.agent_id, Request.attempt)
                .where(Request.id == int(result.req_id))
                .with_for_update()
            )
        ).first()
        if _is_stale(row, agent_id, int(result.attempt)):
            return 0
        # 持有 pyp 行锁跨库写数据：先数据后状态的顺序不变（SDD §4.4）
        written = await Ingestor(engine_dc).upsert(
            table, result.items, batch_id=int(result.batch_id), fingerprint_keys=fingerprint_keys
        )
        source_id = (
            await conn.execute(
                select(Task.source_id)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .where(Request.id == int(result.req_id))
            )
        ).scalar()
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == int(result.req_id))
            .values(
                state=int(RequestState.SUCCESS),
                lease_until=None,  # 完成即释放租约，免遭 reaper 回收
                not_before=None,
                error_code=None,
                reason_code=None,
                error_detail=None,
                retry_after_s=None,
                response_status=s.response_status,
                count_ok=int(s.count_ok),
                count_fail=int(s.count_fail),
                count_blank=int(s.count_blank),
                duration_ms=int(s.elapsed_s * 1000),
            )
        )
        if source_id is not None:
            await conn.execute(
                update(Source.__table__)
                .where(Source.id == source_id)
                .values(
                    last_status_code=s.response_status,
                    last_success_at=func.now(),
                    consecutive_failures=0,
                    cooldown_until=case((Source.cooldown_until <= func.now(), None), else_=Source.cooldown_until),
                    cooldown_reason=case((Source.cooldown_until <= func.now(), None), else_=Source.cooldown_reason),
                )
            )
    return written


async def set_request_state(
    engine_pyp: AsyncEngine,
    req_id: int,
    state: int,
    *,
    agent_id: str | None = None,
    attempt: int | None = None,
    reason_code: str | None = None,
    message: str | None = None,
) -> int:
    """置请求状态（正=正常态、负=错误码）。失败/取消回报走此。终态一律释放租约。

    fencing（P0-10）：只有未终结（QUEUED/ASSIGNED/RUNNING）的请求可被置态——成功/取消/失败后的
    迟到回报一律不覆盖；传 agent_id/attempt 时归属与代次也须吻合。返回受影响行数。
    """
    conds = [
        Request.id == req_id,
        Request.state.in_((int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING))),
    ]
    if agent_id is not None:
        conds.append(Request.agent_id == agent_id)
    if attempt is not None:
        conds.append(Request.attempt == attempt)
    values: dict = {
        "state": state,
        "error_code": state if state < 0 else None,
        "lease_until": None,
        "not_before": None,
    }
    if reason_code is not None:
        values["reason_code"] = reason_code[:64]
    if message is not None:
        values["error_detail"] = message[:1000]
    async with engine_pyp.begin() as conn:
        res = await conn.execute(update(Request.__table__).where(*conds).values(**values))
    return res.rowcount


_RETRYABLE_ERRORS = frozenset(
    {int(ErrorCode.NETWORK), int(ErrorCode.TIMEOUT), int(ErrorCode.THROTTLED), int(ErrorCode.UPSTREAM)}
)


def _retry_delay(error_code: int, attempt: int, requested_s: float | None) -> int:
    """计算受控退避：尊重 Retry-After，但统一限制在 1 秒到 1 小时。"""
    if requested_s is not None:
        return min(3600, max(1, math.ceil(requested_s)))
    base = 30 if error_code == int(ErrorCode.THROTTLED) else 5
    return min(300, base * 2 ** min(max(attempt, 0), 6))


async def defer_request_for_retry(
    engine_pyp: AsyncEngine,
    req_id: int,
    error_code: int,
    *,
    retry_after_s: float | None = None,
    response_status: int | None = None,
    reason_code: str | None = None,
    message: str | None = None,
    max_attempt: int = 3,
    agent_id: str | None = None,
    attempt: int | None = None,
) -> tuple[str | None, bool]:
    """把可恢复失败延迟重排；达到源/全局上限则定格失败。

    返回 ``(source_uuid, requeued)``。429/5xx 同时设置源级冷却，确保同源其他请求也不会在
    Retry-After 窗口内抢跑；网络类故障只延迟当前请求。
    传 agent_id/attempt 时按 fencing 守卫（P0-10）：迟到/越权失败回报不消耗新代次的重试预算。
    """
    if error_code not in _RETRYABLE_ERRORS:
        raise ValueError(f"error_code {error_code} 不是可重试错误")
    now = datetime.now(UTC)
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(
                    Request.attempt,
                    Request.state,
                    Request.agent_id,
                    Request.batch_id,
                    Source.id.label("source_id"),
                    Source.uuid,
                    Source.retry,
                )
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == req_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            return None, False
        if row.state not in {int(RequestState.ASSIGNED), int(RequestState.RUNNING)}:
            # 重复/迟到回报不得再次消耗重试预算。已经回队则维持“已重排”语义。
            return str(row.uuid), row.state == int(RequestState.QUEUED)
        if agent_id is not None and row.agent_id != agent_id:
            return str(row.uuid), True  # 越权/迟到（请求已重派给他人）：不动新代次
        if attempt is not None and int(row.attempt or 0) != attempt:
            return str(row.uuid), True  # 代次不符：旧代次的失败不消耗新代次预算
        next_attempt = int(row.attempt or 0) + 1
        attempt_limit = max(1, min(max_attempt, int(row.retry or max_attempt)))
        delay_s = _retry_delay(error_code, int(row.attempt or 0), retry_after_s)
        not_before = now + timedelta(seconds=delay_s)
        requeued = next_attempt < attempt_limit
        detail = (message or "")[:1000] or None
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(
                state=int(RequestState.QUEUED) if requeued else error_code,
                attempt=next_attempt,
                not_before=not_before if requeued else None,
                lease_until=None,
                agent_id=None,
                error_code=error_code,
                response_status=response_status,
                reason_code=(reason_code or "retryable_failure")[:64],
                error_detail=detail,
                retry_after_s=delay_s,
            )
        )
        source_values: dict = {
            "last_status_code": response_status,
            "last_failure_at": func.now(),
            "consecutive_failures": Source.consecutive_failures + 1,
        }
        if error_code in {int(ErrorCode.THROTTLED), int(ErrorCode.UPSTREAM)}:
            extend_cooldown = (Source.cooldown_until.is_(None)) | (Source.cooldown_until < not_before)
            source_values.update(
                cooldown_until=case((extend_cooldown, not_before), else_=Source.cooldown_until),
                cooldown_reason=case((extend_cooldown, (reason_code or "backoff")[:64]), else_=Source.cooldown_reason),
            )
        await conn.execute(update(Source.__table__).where(Source.id == row.source_id).values(**source_values))
        await conn.execute(
            TaskEvent.__table__.insert().values(
                batch_id=row.batch_id,
                type="request.deferred" if requeued else "request.retry_exhausted",
                payload={
                    "req_id": req_id,
                    "error_code": error_code,
                    "reason_code": reason_code,
                    "response_status": response_status,
                    "retry_after_s": delay_s,
                    "attempt": next_attempt,
                },
            )
        )
    return str(row.uuid), requeued


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


async def finalize_request_batch(engine_pyp: AsyncEngine, req_id: int) -> int | None:
    """尝试收尾请求所属批次；仅在本次完成 ``running -> done`` 时返回批次 id。"""
    async with engine_pyp.connect() as conn:
        batch_id = (await conn.execute(select(Request.batch_id).where(Request.id == req_id))).scalar()
    if batch_id is None:
        return None
    return int(batch_id) if await finalize_batch_if_done(engine_pyp, int(batch_id)) else None


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
    node_token_hash: str | None = None,
) -> tuple[int, str | None]:
    """注册/重连时 upsert agents 行（status=online、刷新 last_heartbeat/能力/槽位）。

    node_token_hash 仅在**新签发凭证**时传入并覆盖；重连（凭 node_token 认证）传 None，
    不得清掉既有凭证 hash（P0-07：凭证生命周期闭环）。
    返回 (weight, group_name)——由管理员在库中预置，回灌 hub 用于加权/分组派发；新节点默认 weight=1。
    """
    values: dict = {
        "agent_id": agent_id,
        "hostname": hostname,
        "slot_n": slot_n,
        "capabilities": capabilities,
        "status": "online",
        "last_heartbeat": func.now(),
    }
    updates = {k: v for k, v in values.items() if k != "agent_id"}
    if node_token_hash is not None:
        values["node_token_hash"] = node_token_hash
        updates["node_token_hash"] = node_token_hash
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(Agent.__table__).values(**values).on_conflict_do_update(index_elements=["agent_id"], set_=updates)
        )
        row = (await conn.execute(select(Agent.weight, Agent.group_name).where(Agent.agent_id == agent_id))).first()
    return (row[0] if row else 1), (row[1] if row else None)


async def auth_node(engine_pyp: AsyncEngine, token_hash: str) -> str | None:
    """按长期节点凭证 hash 找回 agent_id（重连认证，P0-07）；无匹配返回 None。"""
    async with engine_pyp.connect() as conn:
        return (await conn.execute(select(Agent.agent_id).where(Agent.node_token_hash == token_hash).limit(1))).scalar()


async def source_rate_limits(engine_pyp: AsyncEngine) -> dict[str, int]:
    """有 running 批次的各源的 rate_limit（req/s）：{source_uuid: rate_limit}，供派发环限流。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Source.uuid, Source.rate_limit)
                .select_from(Source.__table__)
                .join(Task.__table__, Task.source_id == Source.id)
                .join(Batch.__table__, Batch.task_id == Task.id)
                .where(
                    Batch.status == "running",
                    Source.access_confirmed_at.is_not(None),
                    Source.paused_at.is_(None),
                    (Source.cooldown_until.is_(None)) | (Source.cooldown_until <= func.now()),
                )
                .distinct()
            )
        ).all()
    return {u: int(r) for u, r in rows}


async def source_of_request(engine_pyp: AsyncEngine, req_id: int) -> str | None:
    """由 req_id 反解数据源短码（供 AIMD 回退信号定位数据源）。"""
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


async def pause_source_for_request(
    engine_pyp: AsyncEngine,
    req_id: int,
    reason: str | None = None,
    *,
    response_status: int | None = None,
    reason_code: str | None = None,
) -> tuple[str | None, list[str], list[int]]:
    """因访问拒绝暂停整源，并终止该源所有尚未派发的请求。

    返回 ``(source_uuid, 其他在途 req_id, running batch_id)``；调用方负责取消在途任务并收尾已无在途请求的批次。
    """
    message = (reason or "目标系统拒绝访问，等待人工复核")[:1000]
    async with engine_pyp.begin() as conn:
        source = (
            await conn.execute(
                select(Source.id, Source.uuid, Request.batch_id)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == req_id)
            )
        ).first()
        if source is None:
            return None, [], []
        source_id, source_uuid, trigger_batch_id = source
        batch_ids = list(
            (
                await conn.execute(
                    select(Batch.id)
                    .join(Task.__table__, Batch.task_id == Task.id)
                    .where(Task.source_id == source_id, Batch.status == "running")
                )
            )
            .scalars()
            .all()
        )
        other_inflight = list(
            (
                await conn.execute(
                    select(Request.id).where(
                        Request.batch_id.in_(batch_ids),
                        Request.state.in_(_INFLIGHT),
                        Request.id != req_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            update(Source.__table__)
            .where(Source.id == source_id)
            .values(
                paused_at=func.now(),
                pause_reason=message,
                cooldown_until=None,
                cooldown_reason=None,
                last_status_code=response_status,
                last_failure_at=func.now(),
                consecutive_failures=Source.consecutive_failures + 1,
            )
        )
        await conn.execute(
            update(Request.__table__)
            .where(
                Request.batch_id.in_(batch_ids),
                (Request.state == int(RequestState.QUEUED)) | (Request.id == req_id),
            )
            .values(
                state=int(ErrorCode.ACCESS_PAUSED),
                error_code=int(ErrorCode.ACCESS_PAUSED),
                lease_until=None,
                not_before=None,
            )
        )
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(
                response_status=response_status,
                reason_code=(reason_code or "access_review_required")[:64],
                error_detail=message,
            )
        )
        await conn.execute(
            TaskEvent.__table__.insert().values(
                batch_id=trigger_batch_id,
                type="source.access_paused",
                payload={
                    "source": str(source_uuid),
                    "req_id": req_id,
                    "response_status": response_status,
                    "reason_code": reason_code,
                },
            )
        )
    return str(source_uuid), [str(value) for value in other_inflight], [int(value) for value in batch_ids]


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
    同轮内仍按三元 score（07 定案）：(优先级档, 深度升序=BFS, 入队序)。
    caps 非 None 时按在线能力过滤（见 :func:`_cap_filter`），队头缺能力的请求不占窗口。
    """
    if caps is not None and not caps:
        return []  # 无空闲节点，无需扫描
    rr = func.row_number().over(  # 每源内的名次：跨源轮转用
        partition_by=Source.uuid, order_by=(_PRIORITY_RANK, Request.depth, Request.created_at, Request.id)
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
            .order_by(rr, _PRIORITY_RANK, Request.depth, Request.created_at, Request.id)
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
        account = params.get("account")
        if not isinstance(account, str):
            account = None
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
                timeout_s=max(1, int(timeout_s or 30)),
                attempt=int(attempt or 0),
                engine_hint=engine_hint,
                group=task_group or source_group,
                account=account,
                archive_raw=bool(raw_archive),
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
                select(
                    Request.batch_id,
                    Request.depth,
                    Request.rule_id,
                    Request.rule_hash,
                    Request.rule_version,
                    Batch.status,
                    Source.access_confirmed_at,
                    Source.paused_at,
                )
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == parent_req_id)
            )
        ).first()
        if parent is None:
            return 0
        batch_id, parent_depth, rule_id, rule_hash, rule_version, batch_status, confirmed_at, paused_at = parent
        if batch_status != "running":
            return 0  # 批次已取消/收尾：续爬子请求不得再入队（否则 canceling 批次永远收不了口）
        if confirmed_at is None or paused_at is not None:
            return 0
        child_depth = (parent_depth or 0) + 1
        spec = None
        if rule_id:
            spec = (await conn.execute(select(Rule.spec).where(Rule.id == rule_id))).scalar()
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
                    rule_id=rule_id,
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
                    Source.access_confirmed_at.is_not(None),
                    Source.paused_at.is_(None),
                    (Source.cooldown_until.is_(None)) | (Source.cooldown_until <= func.now()),
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


async def claim_schedule(engine_pyp: AsyncEngine, schedule_id: int, next_run_at: datetime) -> bool:
    """原子认领一次到期触发：仅当调度仍启用且仍到期时推进 next_run_at，返回是否认领成功。

    认领成功者才建批次（DB-010）：多进程/重复 tick 对同一到期时间点只会有一个赢家；
    建批次前崩溃最多漏触发一次（cron 语义可接受），不会重复触发。
    """
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Schedule.__table__)
            .where(
                Schedule.id == schedule_id,
                Schedule.enabled.is_(True),
                (Schedule.next_run_at.is_(None)) | (Schedule.next_run_at <= func.now()),
            )
            .values(next_run_at=next_run_at)
        )
    return bool(res.rowcount)


async def disable_schedule(engine_pyp: AsyncEngine, schedule_id: int) -> None:
    """停用调度（如 cron 表达式非法）；避免坏调度每 tick 反复到期。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Schedule.__table__).where(Schedule.id == schedule_id).values(enabled=False))


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


async def rerun_source(engine_pyp: AsyncEngine, source_uuid: str, *, channel: Channel = Channel.PROD) -> int:
    """按数据源存档配置（task.params 里的种子 + 当前 active 规则）重跑一次，返回新批次 id。

    无需重新提交种子/规则（07 定时触发同源）。访问策略闸门在 create_batch_with_requests 内生效
    （未确认/暂停源抛 PermissionError）。缺任务或缺种子抛 LookupError。
    """
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(Task.id, Task.params)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Source.uuid == source_uuid)
                .order_by(Task.id)
                .limit(1)
            )
        ).first()
    if row is None:
        raise LookupError(f"数据源 {source_uuid!r} 无关联任务，无法重跑")
    seeds = list((row[1] or {}).get("seed_urls") or [])
    if not seeds:
        raise LookupError(f"数据源 {source_uuid!r} 未存档种子 URL（task.params.seed_urls 为空），无法重跑")
    batch_id, _ = await create_batch_for_task(
        engine_pyp, task_id=row[0], source_uuid=source_uuid, seed_urls=seeds, channel=channel
    )
    return batch_id


async def source_field_names(engine_pyp: AsyncEngine, source_uuid: str) -> list[str]:
    """数据源当前规则声明的字段名（供 CSV 导出的稳定列序）；无规则返回空列表。"""
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(Rule.spec)
                .join(Source.__table__, Rule.source_id == Source.id)
                .where(Source.uuid == source_uuid)
                .order_by(Rule.version.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        return []
    try:
        return [f["name"] for f in (row[0] or {}).get("fields", []) if f.get("name")]
    except AttributeError, TypeError:
        return []
