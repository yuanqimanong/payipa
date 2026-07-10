"""审计写入（03 §2.5：`pyp.audit_log` 覆盖 SQL 查询、规则发布、强制入库、权限变更…）。

形状对齐 contracts `AuditEvent`（actor/action/object/前后值/来源；时间由 TimestampMixin 落库补）。
数据源访问复核、管理操作和后续功能统一复用本模块。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import AuditLog

log = logging.getLogger(__name__)


async def record_audit(
    engine_pyp: AsyncEngine,
    *,
    action: str,
    actor_id: int | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    source: str | None = None,
) -> None:
    """落一条审计（如 action="sql.query"）。抛异常由调用方决定成败语义。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(AuditLog.__table__).values(
                actor_id=actor_id,
                action=action,
                object_type=object_type,
                object_id=object_id,
                before=before,
                after=after,
                source=source,
            )
        )


async def record_audit_best_effort(engine_pyp: AsyncEngine, *, action: str, **kw: Any) -> None:
    """best-effort 变体：审计失败只记日志，不影响主流程（与触发器 best-effort 语义一致）。"""
    try:
        await record_audit(engine_pyp, action=action, **kw)
    except Exception as exc:  # noqa: BLE001 —— 审计不可用不应放大为业务故障
        log.warning("audit_write_failed action=%s error=%s", action, exc)
