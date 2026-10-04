"""采集兼容入口。新代码直接依赖 sources/batches/dispatch/results 等职责模块。

保留原有公开函数、签名与返回值；业务实现不再放在此模块。
"""

from __future__ import annotations

from payipa.crawl._policy import ACCESS_BASES, url_fingerprint
from payipa.crawl.batches import (
    batch_progress,
    batch_trigger_context,
    cancel_batch,
    create_batch_for_task,
    create_batch_with_requests,
    finalize_batch_if_done,
    finalize_request_batch,
    rerun_source,
    sweep_canceling_batches,
)
from payipa.crawl.dispatch import (
    claim_queued_for_dispatch,
    mark_assigned,
    mark_running,
    queue_depth,
    requeue_agent_inflight,
    requeue_expired_leases,
    requeue_request,
)
from payipa.crawl.frontier import enqueue_discovered
from payipa.crawl.nodes import (
    auth_node,
    enroll_agent,
    issue_agent_enrollment,
    register_agent,
    revoke_agent_credential,
    set_agent_offline,
    touch_agent,
)
from payipa.crawl.resilience import defer_request_for_retry
from payipa.crawl.results import (
    ResultCommit,
    commit_result,
    fence_ok,
    handle_result,
    resolve_ingest_context,
    set_request_state,
)
from payipa.crawl.schedules import advance_schedule, claim_schedule, disable_schedule, due_schedules
from payipa.crawl.sources import (
    ensure_data_table,
    pause_source_for_request,
    review_source_access,
    setup_source,
    source_field_names,
    source_of_request,
    source_rate_limits,
)

__all__ = [
    "ACCESS_BASES",
    "ResultCommit",
    "advance_schedule",
    "auth_node",
    "batch_progress",
    "batch_trigger_context",
    "cancel_batch",
    "claim_queued_for_dispatch",
    "claim_schedule",
    "commit_result",
    "create_batch_for_task",
    "create_batch_with_requests",
    "defer_request_for_retry",
    "disable_schedule",
    "due_schedules",
    "enqueue_discovered",
    "enroll_agent",
    "ensure_data_table",
    "fence_ok",
    "finalize_batch_if_done",
    "finalize_request_batch",
    "handle_result",
    "issue_agent_enrollment",
    "mark_assigned",
    "mark_running",
    "pause_source_for_request",
    "queue_depth",
    "register_agent",
    "requeue_agent_inflight",
    "requeue_expired_leases",
    "requeue_request",
    "rerun_source",
    "resolve_ingest_context",
    "review_source_access",
    "revoke_agent_credential",
    "set_agent_offline",
    "set_request_state",
    "setup_source",
    "source_field_names",
    "source_of_request",
    "source_rate_limits",
    "sweep_canceling_batches",
    "touch_agent",
    "url_fingerprint",
]
