"""后台派发环 + 租约回收（M2）。

PG 为权威、内存 hub 仅运行态视图：每 interval 秒 (1) 回收到期租约的在途请求 →
(2) 把 QUEUED 请求填到当前空闲槽（跨所有在线 agent 按空闲槽公平铺满）。
单个 anyio 后台任务，挂 FastAPI lifespan，随服务优雅启停（结构化并发，整树可取消）。

不直接碰 payipa-core 的 ORM/SQL——只调用 payipa.crawl.run 的 helper（server→core→contracts 不破）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import anyio
from cronsim import CronSim, CronSimError
from payipa.crawl.run import (
    advance_schedule,
    claim_queued_for_dispatch,
    create_batch_for_task,
    due_schedules,
    mark_assigned,
    requeue_expired_leases,
    requeue_request,
    source_rate_limits,
    sweep_canceling_batches,
)
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.security.tokens import issue_upload_token
from payipa_contracts import TaskAssign

from pyp_server.settings import get_server_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from pyp_server.hub import AgentHub
    from pyp_server.ratelimit import SourceRateLimiter

logger = logging.getLogger("pyp_server.scheduler")


async def drain_once(hub: AgentHub, pyp: AsyncEngine, secret: str, lease_s: int, limiter: SourceRateLimiter) -> int:
    """把 QUEUED 请求填到匹配的空闲槽，直到无槽/无排队/本轮无进展。返回本轮成功下发数。

    先按 (优先级,深度,序) 取一批候选，逐条选**同组**空闲节点（分组亲和 + 空闲槽/权重择优）；每条还要过**每源限流**
    令牌桶（红线3）。某条无匹配节点或被限流则跳过换下一条，避免头阻塞。on_dispatched 递减空闲槽，故多 agent 铺满。
    """
    rates = await source_rate_limits(pyp)
    dispatched = 0
    while True:
        if hub.pick_free() is None:  # 全无空闲槽 → 本轮到此
            return dispatched
        specs = await claim_queued_for_dispatch(pyp, limit=32)
        if not specs:  # 无排队请求
            return dispatched
        progressed = False
        for spec in specs:
            conn = hub.pick_free(spec.group)  # 同组空闲节点（group=None 可派任意）
            if conn is None:
                continue  # 该任务分组暂无空闲节点 → 跳过，换下一条（不阻塞别组）
            if not limiter.take(spec.source, rates.get(spec.source, 0)):
                continue  # 该源本 tick 令牌用尽 → 留排队，下一 tick 再派（每源限流）
            req_id = int(spec.req_id)
            lease_until = datetime.now(UTC) + timedelta(seconds=spec.timeout_s or lease_s)
            if await mark_assigned(pyp, req_id, conn.agent_id, lease_until) != 1:
                continue  # 未抢到（状态已变）——试下一条
            token = issue_upload_token(secret, spec.source, int(spec.batch_id))
            try:
                await hub.send_frame(conn.agent_id, TaskAssign(task=spec, upload_token=token))
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
    """把到点的 cron 调度实例化成新批次；用 cronsim 从 now 推进 next_run_at。返回触发的批次数。"""
    fired = 0
    for sched_id, task_id, cron_expr, source_uuid, seeds in await due_schedules(pyp):
        if not seeds:  # 无存档种子（未经建源流程）→ 只推进时间、不空跑
            logger.warning("schedule %s (task %s) has no seed_urls; skip firing", sched_id, task_id)
        else:
            await create_batch_for_task(pyp, task_id=task_id, source_uuid=source_uuid, seed_urls=seeds)
            fired += 1
        try:
            next_run = next(CronSim(cron_expr, now))
        except CronSimError, StopIteration:
            logger.warning("schedule %s bad cron %r; disable by leaving next_run_at as-is", sched_id, cron_expr)
            continue
        await advance_schedule(pyp, sched_id, next_run)
    return fired


async def dispatch_loop(app: FastAPI) -> None:
    """长驻后台环：触发到点 cron → 回收过期租约 → 排空队列。任何业务异常都不能让它退出（仅 cancel 时结束）。"""
    settings = get_server_settings()
    hub: AgentHub = app.state.hub
    limiter: SourceRateLimiter = app.state.limiter
    pyp = get_engine("pyp")
    secret = get_db_settings().upload_secret
    interval, lease_s, max_attempt = settings.dispatch_interval_s, settings.task_lease_s, settings.max_attempt
    logger.info("dispatch loop up (interval=%ss lease=%ss max_attempt=%s)", interval, lease_s, max_attempt)
    fails = 0
    while True:
        try:
            await fire_due_schedules(pyp, datetime.now(UTC))
            await requeue_expired_leases(pyp, max_attempt=max_attempt)
            await sweep_canceling_batches(pyp)
            await drain_once(hub, pyp, secret, lease_s, limiter)
            fails = 0
        except Exception:  # noqa: BLE001 —— anyio 取消是 BaseException，不会被这里吞掉
            fails += 1
            delay = min(30.0, interval * 2 ** min(fails - 1, 5))  # 指数退避，DB 抖动时不忙转拖垮连接池
            logger.exception("dispatch loop tick failed (x%d); backoff %.1fs", fails, delay)
            await anyio.sleep(delay)  # 取消点
            continue
        await anyio.sleep(interval)  # 取消点：lifespan 关停时在此优雅退出
