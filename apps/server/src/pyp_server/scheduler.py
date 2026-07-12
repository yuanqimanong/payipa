"""后台派发环 + 租约回收（M2）。

PG 为权威、内存 hub 仅运行态视图：每 interval 秒 (1) 回收到期租约的在途请求 →
(2) 把 QUEUED 请求填到当前空闲槽（跨所有在线 agent 按空闲槽公平铺满）。
单个 anyio 后台任务，挂 FastAPI lifespan，随服务优雅启停（结构化并发，整树可取消）。

不直接碰 payipa-core 的 ORM/SQL——只调用 payipa.crawl.run 的 helper（server→core→contracts 不破）。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anyio
from cronsim import CronSim, CronSimError
from payipa.crawl.run import (
    claim_queued_for_dispatch,
    claim_schedule,
    create_batch_for_task,
    disable_schedule,
    due_schedules,
    mark_assigned,
    requeue_expired_leases,
    requeue_request,
    source_rate_limits,
    sweep_canceling_batches,
)
from payipa.db.dynamic_schema import reconcile_data_schemas
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.security.tokens import issue_rule_token, issue_upload_token
from payipa.storage import gc_expired_artifacts, get_storage
from payipa_contracts import TaskAssign

from pyp_server.settings import get_server_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from pyp_server.hub import AgentHub
    from pyp_server.ratelimit import SourceRateLimiter

logger = logging.getLogger("pyp_server.scheduler")


async def drain_once(hub: AgentHub, pyp: AsyncEngine, secret: str, ack_s: int, limiter: SourceRateLimiter) -> int:
    """把 QUEUED 请求填到匹配的空闲槽，直到无槽/无排队/本轮无进展。返回本轮成功下发数。

    派发只给 ACK 短租（ack_s 秒）：TaskAssign 丢失时请求快速被 reaper 回收重派，
    agent 回 TaskAck 后（ws.mark_running）才展成完整执行租约（P0-10）。

    候选查询在 SQL 侧先做**源轮转 + 在线能力过滤**（P0-11：单源积压或队头缺能力节点都不再饿死后续），
    取回后逐条选**同组**空闲节点（分组亲和 + 空闲槽/权重择优）；每条还要过**每源限流**
    令牌桶（红线3）。某条无匹配节点或被限流则跳过换下一条，避免头阻塞。on_dispatched 递减空闲槽，故多 agent 铺满。
    """
    rates = await source_rate_limits(pyp)
    dispatched = 0
    while True:
        if hub.pick_free() is None:  # 全无空闲槽 → 本轮到此
            return dispatched
        specs = await claim_queued_for_dispatch(pyp, limit=32, caps=hub.free_caps())
        if not specs:  # 无排队请求
            return dispatched
        progressed = False
        for spec in specs:
            conn = hub.pick_free(spec.group, spec.engine_hint.value)  # 同组且具备目标引擎的空闲节点
            if conn is None:
                continue  # 该任务分组暂无空闲节点 → 跳过，换下一条（不阻塞别组）
            if not limiter.take(spec.source, rates.get(spec.source, 0)):
                continue  # 该源本 tick 令牌用尽 → 留排队，下一 tick 再派（每源限流）
            req_id = int(spec.req_id)
            try:
                # ACK 短租（DB 时钟）；agent 确认后展成执行租约
                if await mark_assigned(pyp, req_id, conn.agent_id, attempt=spec.attempt, lease_s=ack_s) != 1:
                    continue  # 未抢到（状态已变/代次已推进）——试下一条
                token = (
                    issue_upload_token(secret, spec.source, int(spec.batch_id), channel=spec.channel.value)
                    if spec.channel.value == "prod" and spec.archive_raw
                    else None
                )
                rule_token = issue_rule_token(secret, spec.rule_ptr.content_hash, ttl_s=max(300, ack_s + 60))
            except Exception:  # noqa: BLE001 —— 毒丸请求（如 rule_ptr 数据异常）：隔离本条，不拖垮整轮/全部源派发
                logger.exception("prepare dispatch for req %s failed; quarantine this request", req_id)
                # mark_assigned 已置 ASSIGNED 时不回退：ACK 短租到期由 reaper 回收、attempt 累进至 max 后定格
                # NODE_LOST 自然止损；mark_assigned 前失败则仍 QUEUED，仅本条每 tick 重试、不阻塞他源。
                continue
            try:
                await hub.send_frame(
                    conn.agent_id,
                    TaskAssign(task=spec, upload_token=token, rule_token=rule_token),
                )
            except Exception:  # noqa: BLE001 —— 下发失败（连接坏）：退回 QUEUED，结束本轮
                logger.warning("send TaskAssign failed for req %s; requeue", req_id, exc_info=True)
                # requeue 也失败则异常上抛给 dispatch_loop 退避重试；该请求暂留 ASSIGNED，租约 reaper 兜底。
                await requeue_request(pyp, req_id)
                return dispatched
            hub.on_dispatched(conn.agent_id, spec.req_id)
            dispatched += 1
            progressed = True
        if not progressed:  # 本批都无匹配节点/抢失败 → 结束本轮，等下一 tick（不忙转）
            return dispatched


async def fire_due_schedules(pyp: AsyncEngine, now: datetime) -> int:
    """把到点的 cron 调度实例化成新批次；用 cronsim 从 now 推进 next_run_at。返回触发的批次数。

    先认领（claim_schedule 条件推进，DB-010 幂等）再建批次：同一到期时间点只有一个赢家，
    多进程/重复 tick 不会重复建批次。坏 cron 直接停用，不再每 tick 反复到期告警。
    """
    fired = 0
    for sched_id, task_id, cron_expr, source_uuid, seeds in await due_schedules(pyp):
        try:
            next_run = next(CronSim(cron_expr, now))
        except CronSimError, StopIteration:
            logger.warning("schedule %s bad cron %r; disabled", sched_id, cron_expr)
            await disable_schedule(pyp, sched_id)
            continue
        if not await claim_schedule(pyp, sched_id, next_run):
            continue  # 已被其他进程/上一 tick 认领
        if not seeds:  # 无存档种子（未经建源流程）→ 只推进时间、不空跑
            logger.warning("schedule %s (task %s) has no seed_urls; skip firing", sched_id, task_id)
            continue
        await create_batch_for_task(pyp, task_id=task_id, source_uuid=source_uuid, seed_urls=seeds)
        fired += 1
    return fired


async def _run_stage(name: str, coro):
    """跑一个后台阶段并**隔离其异常**：单阶段失败只记录、不影响同轮其它阶段。返回 (结果|None, 是否成功)。

    此前整轮多阶段包在同一 try 里，一条毒丸任务/一次某阶段异常会拖垮全部派发并把 readyz 拖成抖动。
    """
    try:
        return await coro, True
    except Exception:  # noqa: BLE001 —— anyio 取消是 BaseException，不会被这里吞掉
        logger.exception("dispatch stage %r failed", name)
        return None, False


async def dispatch_loop(app: FastAPI) -> None:
    """长驻后台环：cron 触发 → schema 对账 → raw/artifact GC → 回收过期租约 → 清取消 → 排空队列。

    任何业务异常都不能让它退出（仅 cancel 时结束），且各阶段异常互相隔离——一个阶段失败不跳过其余阶段。
    """
    settings = get_server_settings()
    hub: AgentHub = app.state.hub
    limiter: SourceRateLimiter = app.state.limiter
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    storage = get_storage()  # 预检已验证后端可构建；此处取一次供 GC 复用
    secret = get_db_settings().upload_secret
    interval, ack_s, max_attempt = settings.dispatch_interval_s, settings.ack_timeout_s, settings.max_attempt
    gc_interval = settings.gc_interval_s
    logger.info(
        "dispatch loop up (interval=%ss ack=%ss max_attempt=%s gc=%ss)", interval, ack_s, max_attempt, gc_interval
    )
    health = getattr(app.state, "loop_health", {}).get("dispatch")  # readyz 心跳档案（P0-06）
    fails = 0
    next_reconcile = 0.0
    next_gc = 0.0
    while True:
        ok = True
        now = time.monotonic()
        if now >= next_reconcile:  # 低频：动态 schema 对账
            report, sok = await _run_stage("reconcile", reconcile_data_schemas(pyp, dc))
            if sok:
                if report and report.get("checked"):
                    logger.info("dynamic schema reconciliation: %s", report)
                next_reconcile = now + 60.0
            ok = ok and sok
        if gc_interval > 0 and now >= next_gc:  # 低频：清理过期 raw/artifact，防磁盘写满型不可自愈宕机
            removed, sok = await _run_stage("gc", gc_expired_artifacts(dc, storage))
            if sok:
                if removed:
                    logger.info("artifact GC removed %d expired object(s)", removed)
                next_gc = now + gc_interval
            ok = ok and sok
        _, sok = await _run_stage("fire_due_schedules", fire_due_schedules(pyp, datetime.now(UTC)))
        ok = ok and sok
        _, sok = await _run_stage("requeue_expired_leases", requeue_expired_leases(pyp, max_attempt=max_attempt))
        ok = ok and sok
        _, sok = await _run_stage("sweep_canceling_batches", sweep_canceling_batches(pyp))
        ok = ok and sok
        _, sok = await _run_stage("drain_once", drain_once(hub, pyp, secret, ack_s, limiter))
        ok = ok and sok
        if ok:
            fails = 0
            if health is not None:
                health.ok()
            await anyio.sleep(interval)  # 取消点：lifespan 关停时在此优雅退出
        else:
            fails += 1
            if health is not None:
                health.fail(f"{fails} consecutive tick(s) had a failing stage")
            delay = min(30.0, interval * 2 ** min(fails - 1, 5))  # 指数退避，DB 抖动时不忙转拖垮连接池
            logger.warning("dispatch loop tick had failing stage(s) (x%d); backoff %.1fs", fails, delay)
            await anyio.sleep(delay)  # 取消点
