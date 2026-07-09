"""deliver —— 数据推送（05）：事务性 outbox + 主控 Consumer 排空 + 死信 + Dataset API + 通知。

M4 slice-1 已落：`outbox`（push_outbox 状态机 pending→inflight→sent|dead + 幂等去重 + 退避重试 + 租约回收 +
Consumer 排空环 run_outbox_once）。复用 07 的重试/退避/死信语义；PG 为权威。后续切片：真实通道投递器（主控隔离
子进程 + 目标域白名单 + 解密凭证注入）、对外 Dataset API（JSON 行 + keyset）、通知（NotifyBot）。
"""

from __future__ import annotations

from payipa.deliver.outbox import (
    Deliverer,
    claim_due,
    enqueue_push,
    mark_dead,
    mark_failed,
    mark_sent,
    requeue_expired,
    run_outbox_once,
)

__all__ = [
    "Deliverer",
    "claim_due",
    "enqueue_push",
    "mark_dead",
    "mark_failed",
    "mark_sent",
    "requeue_expired",
    "run_outbox_once",
]
