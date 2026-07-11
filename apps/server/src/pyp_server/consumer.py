"""后台推送 Consumer（M4 slice-3c）：outbox 排空环，挂 FastAPI lifespan。

每 interval 秒排空一轮 push_outbox（回收过期租约 → 领取到期 pending → 隔离子进程投递 → sent|退避|死信）。
PG 为权威；主控崩溃后靠租约回收续投（红线：不经 agent Redis 队列）。任何业务异常都不让环退出（仅 cancel 结束）。

投递器由 core 的 make_component_deliverer 构造（server→core→contracts 不破）；解密凭证用 KEK（仅主控 env）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.deliver.component import make_component_deliverer
from payipa.deliver.outbox import run_outbox_once

from pyp_server.settings import get_server_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("pyp_server.consumer")


async def consumer_loop(app: FastAPI) -> None:
    """长驻后台环：排空 push_outbox。业务异常指数退避、不退出；lifespan 关停时在 sleep 处优雅结束。"""
    settings = get_server_settings()
    db = get_db_settings()
    pyp = get_engine("pyp")
    business = get_engine("business")
    deliverer = make_component_deliverer(pyp, business, sign_secret=db.upload_secret, kek=db.cred_kek)
    interval = settings.push_interval_s
    logger.info(
        "push consumer up (interval=%ss lease=%ss max_attempts=%s)",
        interval,
        settings.push_lease_s,
        settings.push_max_attempts,
    )
    health = getattr(app.state, "loop_health", {}).get("consumer")  # readyz 心跳档案（P0-06）
    fails = 0
    while True:
        try:
            sent, failed = await run_outbox_once(
                pyp,
                deliverer,
                max_attempts=settings.push_max_attempts,
                lease_s=settings.push_lease_s,
                limit=settings.push_batch,
            )
            if sent or failed:
                logger.info("outbox drained: sent=%d failed=%d", sent, failed)
            fails = 0
            if health is not None:
                health.ok()
        except Exception as exc:  # noqa: BLE001 —— anyio 取消是 BaseException，不会被吞
            fails += 1
            if health is not None:
                health.fail(f"{type(exc).__name__}: {exc}")
            delay = min(30.0, interval * 2 ** min(fails - 1, 5))
            logger.exception("consumer tick failed (x%d); backoff %.1fs", fails, delay)
            await anyio.sleep(delay)
            continue
        await anyio.sleep(interval)  # 取消点
