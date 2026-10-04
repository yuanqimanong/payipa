"""后台推送 Consumer（M4 slice-3c）：outbox 排空环，挂 FastAPI lifespan。

每 interval 秒排空一轮 push_outbox（回收过期租约 → 领取到期 pending → 隔离子进程投递 → sent|退避|死信）。
PG 为权威；主控崩溃后靠租约回收续投（红线：不经 agent Redis 队列）。任何业务异常都不让环退出（仅 cancel 结束）。

投递器由 core 的 make_component_deliverer 构造（server→core→contracts 不破）；解密凭证用 KEK（仅主控 env）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.deliver.component import make_component_deliverer
from payipa.deliver.outbox import Deliverer, run_outbox_once

from pyp_server.background import run_periodic
from pyp_server.runtime import LoopHealth
from pyp_server.settings import ServerSettings, get_server_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("pyp_server.consumer")


async def consumer_worker(
    pyp: AsyncEngine,
    deliverer: Deliverer,
    settings: ServerSettings,
    *,
    health: LoopHealth | None = None,
) -> None:
    """独立 outbox worker：投递器可替换，重试、心跳和取消采用统一后台执行器。"""
    logger.info(
        "push consumer up (interval=%ss lease=%ss max_attempts=%s)",
        settings.push_interval_s,
        settings.push_lease_s,
        settings.push_max_attempts,
    )

    async def tick() -> bool:
        sent, failed = await run_outbox_once(
            pyp,
            deliverer,
            max_attempts=settings.push_max_attempts,
            lease_s=settings.push_lease_s,
            limit=settings.push_batch,
        )
        if sent or failed:
            logger.info("outbox drained: sent=%d failed=%d", sent, failed)
        return True

    await run_periodic(tick, interval_s=settings.push_interval_s, logger=logger, health=health)


async def consumer_loop(app) -> None:
    """FastAPI lifespan 兼容适配器：构造投递器并交给独立 worker。"""
    settings = getattr(app.state, "settings", None)
    if settings is None:
        settings = get_server_settings()
    db = get_db_settings()
    pyp = get_engine("pyp")
    deliverer = make_component_deliverer(pyp, get_engine("business"), sign_secret=db.upload_secret, kek=db.cred_kek)
    await consumer_worker(pyp, deliverer, settings, health=getattr(app.state, "loop_health", {}).get("consumer"))
