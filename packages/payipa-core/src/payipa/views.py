"""管理界面只读视图查询（M5 前端）：把平台表聚合成 UI 直接可渲染的行 dict。

只读、无副作用；凭证列一律不返回明文（只回是否配置）。分页统一 (limit, offset)。
归属 core（依赖方向 server→core→contracts）；server 视图路由经 RBAC 闸门调用这里。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import (
    Assembly,
    AuditLog,
    Batch,
    LlmModel,
    NotifyBot,
    PushComponent,
    PushOutbox,
    Role,
    RolePermission,
    Rule,
    Schedule,
    Source,
    StorageConfig,
    Task,
    User,
    UserRole,
)


def _iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


async def list_tasks(engine: AsyncEngine, *, limit: int = 100) -> list[dict]:
    """任务清单：join 数据源 + 调度（cron/next_run）+ 最近批次状态。"""
    latest_batch = select(Batch.task_id, func.max(Batch.id).label("bid")).group_by(Batch.task_id).subquery()
    stmt = (
        select(
            Task.id,
            Task.trigger_type,
            Task.priority,
            Task.created_at,
            Source.name.label("source_name"),
            Source.uuid.label("source_uuid"),
            Schedule.cron_expr,
            Schedule.next_run_at,
            Schedule.enabled,
            Batch.status.label("last_status"),
            Batch.finished_at.label("last_finished"),
        )
        .join(Source, Task.source_id == Source.id)
        .outerjoin(Schedule, Schedule.task_id == Task.id)
        .outerjoin(latest_batch, latest_batch.c.task_id == Task.id)
        .outerjoin(Batch, Batch.id == latest_batch.c.bid)
        .order_by(Task.id.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "source_name": r.source_name,
            "source_uuid": r.source_uuid,
            "trigger": r.trigger_type,
            "priority": r.priority,
            "cron": r.cron_expr,
            "next_run": _iso(r.next_run_at),
            "schedule_enabled": r.enabled,
            "last_status": r.last_status,
            "last_finished": _iso(r.last_finished),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def list_audit(engine: AsyncEngine, *, limit: int = 100, offset: int = 0) -> list[dict]:
    """审计日志（最新在前）：actor 关联用户名，敏感 before/after 只回是否有内容。"""
    stmt = (
        select(
            AuditLog.id,
            AuditLog.action,
            AuditLog.object_type,
            AuditLog.object_id,
            AuditLog.source,
            AuditLog.created_at,
            User.username.label("actor"),
        )
        .outerjoin(User, AuditLog.actor_id == User.id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "object_type": r.object_type,
            "object_id": r.object_id,
            "source": r.source,
            "actor": r.actor or "系统",
            "at": _iso(r.created_at),
        }
        for r in rows
    ]


async def list_assemblies(engine: AsyncEngine, *, limit: int = 100) -> list[dict]:
    """组装产物清单：版本 + 状态 + 是否已签名（不回签名值本身）+ 增量开关。"""
    stmt = (
        select(
            Assembly.id,
            Assembly.name,
            Assembly.product_code,
            Assembly.status,
            Assembly.script_ver,
            Assembly.incremental,
            Assembly.signature,
            Assembly.created_at,
        )
        .order_by(Assembly.id.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "product_code": r.product_code,
            "status": r.status,
            "version": r.script_ver,
            "incremental": r.incremental,
            "signed": bool(r.signature),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def list_push_components(engine: AsyncEngine, *, limit: int = 100) -> list[dict]:
    """推送组件清单：版本/状态/签名门/出网白名单域（凭证不回）。"""
    stmt = (
        select(
            PushComponent.id,
            PushComponent.name,
            PushComponent.version,
            PushComponent.status,
            PushComponent.allow_domains,
            PushComponent.signature,
            PushComponent.created_at,
        )
        .order_by(PushComponent.id.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "version": r.version,
            "status": r.status,
            "allow_domains": r.allow_domains or [],
            "signed": bool(r.signature),
            "has_creds": None,  # 凭证是否配置留组件详情页，不在清单泄露形状
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def outbox_summary(engine: AsyncEngine) -> dict[str, int]:
    """推送 outbox 按状态计数（pending/inflight/sent/dead）。"""
    stmt = select(PushOutbox.state, func.count()).group_by(PushOutbox.state)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return dict(rows)


async def list_rules(engine: AsyncEngine, *, limit: int = 200) -> list[dict]:
    """规则清单：按数据源分组的版本 + 状态（内容寻址 hash 前 12 位）。"""
    stmt = (
        select(
            Rule.id,
            Rule.version,
            Rule.status,
            Rule.content_hash,
            Rule.created_at,
            Source.name.label("source_name"),
            Source.uuid.label("source_uuid"),
        )
        .join(Source, Rule.source_id == Source.id)
        .order_by(Source.uuid, Rule.version.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "source_name": r.source_name,
            "source_uuid": r.source_uuid,
            "version": r.version,
            "status": r.status,
            "hash": (r.content_hash or "")[:12],
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def list_users(engine: AsyncEngine, *, limit: int = 200) -> list[dict]:
    """用户清单：状态 + 显示名 + 所属角色名列表（不回密码 hash）。"""
    role_agg = (
        select(UserRole.user_id, func.array_agg(Role.name).label("roles"))
        .join(Role, UserRole.role_id == Role.id)
        .group_by(UserRole.user_id)
        .subquery()
    )
    stmt = (
        select(
            User.id,
            User.username,
            User.display_name,
            User.status,
            User.created_at,
            role_agg.c.roles,
        )
        .outerjoin(role_agg, role_agg.c.user_id == User.id)
        .order_by(User.id)
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "username": r.username,
            "display_name": r.display_name,
            "status": r.status,
            "roles": list(r.roles) if r.roles else [],
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def list_roles(engine: AsyncEngine) -> list[dict]:
    """角色清单：描述 + 权限码计数（管理员为通配 *）。"""
    perm_count = select(RolePermission.role_id, func.count().label("n")).group_by(RolePermission.role_id).subquery()
    stmt = (
        select(Role.id, Role.name, Role.description, perm_count.c.n)
        .outerjoin(perm_count, perm_count.c.role_id == Role.id)
        .order_by(Role.id)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [{"id": r.id, "name": r.name, "description": r.description, "permission_count": r.n or 0} for r in rows]


async def config_overview(engine: AsyncEngine) -> dict:
    """公共配置总览：LLM 模型（不回凭证）、通知机器人、存储后端。"""
    async with engine.connect() as conn:
        models = (
            await conn.execute(
                select(LlmModel.id, LlmModel.name, LlmModel.provider, LlmModel.enabled).order_by(LlmModel.id)
            )
        ).all()
        bots = (await conn.execute(select(NotifyBot.id, NotifyBot.name, NotifyBot.type).order_by(NotifyBot.id))).all()
        stores = (
            await conn.execute(
                select(StorageConfig.id, StorageConfig.name, StorageConfig.backend).order_by(StorageConfig.id)
            )
        ).all()
    return {
        "models": [{"id": m.id, "name": m.name, "provider": m.provider, "enabled": m.enabled} for m in models],
        "notify_bots": [{"id": b.id, "name": b.name, "type": b.type} for b in bots],
        "storage": [{"id": s.id, "name": s.name, "backend": s.backend} for s in stores],
    }


async def storage_overview(engine: AsyncEngine) -> dict:
    """存储总览：登记的存储配置 + 运行时后端类型/磁盘水位 + raw 归档统计。"""
    from payipa.db.data_center import Artifact  # artifacts 固定表属 data_center 库
    from payipa.storage import get_storage

    async with engine.connect() as conn:
        stores = (
            await conn.execute(
                select(StorageConfig.id, StorageConfig.name, StorageConfig.backend).order_by(StorageConfig.id)
            )
        ).all()
    backend = get_storage()
    live = {"backend": type(backend).__name__, "disk_ok": bool(backend.disk_ok())}
    stats: dict = {}
    try:
        from payipa.db.engine import get_engine

        dc = get_engine("data_center")
        async with dc.connect() as conn:
            total, bytes_ = (await conn.execute(select(func.count(), func.coalesce(func.sum(Artifact.size), 0)))).one()
        stats = {"artifacts": int(total), "bytes": int(bytes_)}
    except Exception:  # noqa: BLE001 —— 统计是尽力而为，artifacts 表未建/无 DB 不影响页面
        stats = {}
    return {
        "configs": [{"id": s.id, "name": s.name, "backend": s.backend} for s in stores],
        "live": live,
        "stats": stats,
    }
