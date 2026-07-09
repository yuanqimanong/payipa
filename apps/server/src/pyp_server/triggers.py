"""批次收尾自动触发（M4 slice-5，05 §1.1-1 链路自动 + §1.1-6 进度通知）。

批次由 running→done 的**唯一那次**转变时调用：按所属任务 params 的绑定，
①有 push 组件绑定 → 入 outbox 一条数据集增量推送（Consumer 隔离子进程投递）；
②有通知机器人绑定 → 发一条收尾通知（best-effort，失败只记日志不影响采集）。

三触发统一走 outbox（推送）；通知走内置轻量渠道。全部 best-effort：任何失败都不得让结果入库/收尾回滚。
"""

from __future__ import annotations

import json
import logging

from payipa.crawl.run import batch_trigger_context
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.deliver.notify import NotifyError, notify
from payipa.deliver.outbox import enqueue_push

logger = logging.getLogger("pyp_server.triggers")


async def on_batch_finalized(batch_id: int) -> None:
    """批次收尾钩子：按任务绑定触发自动推送 + 通知。全 best-effort（不抛，异常只记日志）。"""
    pyp = get_engine("pyp")
    try:
        ctx = await batch_trigger_context(pyp, batch_id)
    except Exception:  # noqa: BLE001 —— 上下文读取失败不影响采集主链
        logger.warning("batch %s trigger context load failed", batch_id, exc_info=True)
        return
    if ctx is None:
        return
    params = ctx["params"]

    # ① 链路自动推送：入 outbox（幂等键 = batch-<id>，同批只入一次）
    pc = params.get("push_component_id")
    product = params.get("product_code")
    if pc and product:
        try:
            payload = json.dumps({"kind": "dataset", "product_code": str(product)})
            await enqueue_push(
                pyp,
                component_id=int(pc),
                payload_ref=payload,
                idempotency_key=f"batch-{batch_id}",
                batch_id=batch_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("batch %s auto-push enqueue failed (component=%s)", batch_id, pc, exc_info=True)

    # ② 收尾通知：best-effort
    bot = params.get("notify_bot_id")
    if bot:
        try:
            await notify(
                pyp,
                int(bot),
                title=f"批次 {batch_id} {ctx['status']}",
                text=f"成功 {ctx['ok']}/{ctx['total']}（任务 {ctx['task_id']}）",
                kek=get_db_settings().cred_kek,
            )
        except NotifyError, Exception:  # noqa: BLE001 —— 通知失败不影响采集
            logger.warning("batch %s notify bot %s failed", batch_id, bot, exc_info=True)
