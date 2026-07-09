"""deliver —— 数据推送（05）：事务性 outbox + 主控 Consumer 排空 + 死信 + Dataset API + 通知。

- slice-1 `outbox`：push_outbox 状态机 pending→inflight→sent|dead + 幂等去重 + 退避重试 + 租约回收 + 排空环。
- slice-2 `dataset`：对外只读 Dataset API（API Key + scope + keyset JSON 分页）。
- slice-3 真实通道投递器：`pushexec`（隔离子进程 + 目标域白名单 + env 擦洗）+ `component`（组件登记/签名门 +
  outbox 投递器工厂 make_component_deliverer，注入解密凭证）。复用 07 的重试/退避/死信语义；PG 为权威。
后续：通知（NotifyBot）。
"""

from __future__ import annotations

from payipa.deliver.component import (
    PushComponentStore,
    assert_component_runnable,
    component_content_hash,
    make_component_deliverer,
    sign_component,
    verify_component_signature,
)
from payipa.deliver.dataset import (
    api_key_allows_dataset,
    create_api_key,
    read_dataset,
    verify_api_key,
)
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
from payipa.deliver.pushexec import PushResult, run_push_component

__all__ = [
    "Deliverer",
    "PushComponentStore",
    "PushResult",
    "api_key_allows_dataset",
    "assert_component_runnable",
    "claim_due",
    "component_content_hash",
    "create_api_key",
    "enqueue_push",
    "make_component_deliverer",
    "mark_dead",
    "mark_failed",
    "mark_sent",
    "read_dataset",
    "requeue_expired",
    "run_outbox_once",
    "run_push_component",
    "sign_component",
    "verify_api_key",
    "verify_component_signature",
]
