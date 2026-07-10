"""RBAC 权限矩阵（M5，红线：凭证/权限分层）。

标准 RBAC：用户经角色（user_roles → role_permissions）获得权限，另可经 user_permissions 直授。
`管理员` 角色持通配权限 ``*`` = 超级用户（放行一切）。权限码为稳定字符串（加法演进）。

解析与落库属 pyp 平台库逻辑，放 core；server 侧 require_perm 依赖调用本模块（server→core 不破）。
枚举/默认矩阵在此定义（SDD §14「错误码枚举细化/权限矩阵」实现期产出）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import Permission, Role, RolePermission, User, UserPermission, UserRole

WILDCARD = "*"  # 超级用户通配权限

# 权限目录（code → 说明）。按「资源.动作」命名；新增能力时加码。
PERMISSIONS: dict[str, str] = {
    WILDCARD: "超级用户：放行一切",
    "sources.read": "查看数据源",
    "sources.write": "创建/编辑数据源",
    "sources.run": "触发采集运行",
    "tasks.read": "查看任务/批次",
    "tasks.cancel": "取消批次",
    "rules.read": "查看爬虫规则",
    "rules.write": "编辑爬虫规则",
    "rules.publish": "发布/签名规则",
    "assemblies.read": "查看数据组装",
    "assemblies.write": "编辑组装脚本",
    "assemblies.publish": "发布/签名组装",
    "data.read": "查看采集/组装数据",
    "datasets.read": "读对外数据集 API",
    "push.read": "查看推送组件/记录",
    "push.enqueue": "手动触发推送",
    "push.manage": "编辑/发布推送组件、管理机器人",
    "nodes.read": "查看节点",
    "nodes.manage": "管理节点（权重/分组/下线）",
    "monitor.read": "查看系统监控",
    "storage.manage": "管理对象存储配置",
    "llm.manage": "管理 AI 模型/提示词网关",
    "config.manage": "管理公共配置",
    "users.manage": "管理用户",
    "roles.manage": "管理角色与权限",
    "audit.read": "查看操作日志",
    "force_insert": "高危：强制写入",
}

# 默认角色 → 权限码（Role.name 注释即 管理员/技术/运营/运维）。
DEFAULT_ROLES: dict[str, list[str]] = {
    "管理员": [WILDCARD],
    "技术": [
        "sources.read",
        "sources.write",
        "sources.run",
        "rules.read",
        "rules.write",
        "rules.publish",
        "assemblies.read",
        "assemblies.write",
        "assemblies.publish",
        "push.read",
        "push.enqueue",
        "push.manage",
        "data.read",
        "datasets.read",
        "tasks.read",
        "tasks.cancel",
        "monitor.read",
        "config.manage",
    ],
    "运营": [
        "sources.read",
        "sources.run",
        "tasks.read",
        "tasks.cancel",
        "rules.read",
        "assemblies.read",
        "data.read",
        "datasets.read",
        "push.read",
        "push.enqueue",
        "monitor.read",
    ],
    "运维": [
        "nodes.read",
        "nodes.manage",
        "monitor.read",
        "storage.manage",
        "config.manage",
        "audit.read",
        "tasks.read",
    ],
}


def has_permission(perms: set[str], code: str) -> bool:
    """通配 ``*`` 或精确命中即放行。"""
    return WILDCARD in perms or code in perms


async def effective_permissions(engine_pyp: AsyncEngine, user_id: int) -> set[str]:
    """用户有效权限 = 经角色(role_permissions) ∪ 直授(user_permissions)。含 ``*`` 即超级用户。"""
    async with engine_pyp.connect() as conn:
        via_role = (
            (
                await conn.execute(
                    select(Permission.code)
                    .select_from(UserRole.__table__)
                    .join(RolePermission.__table__, RolePermission.role_id == UserRole.role_id)
                    .join(Permission.__table__, Permission.id == RolePermission.permission_id)
                    .where(UserRole.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        direct = (
            (
                await conn.execute(
                    select(Permission.code)
                    .select_from(UserPermission.__table__)
                    .join(Permission.__table__, Permission.id == UserPermission.permission_id)
                    .where(UserPermission.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    return set(via_role) | set(direct)


# ── 落库 / 播种（幂等）─────────────────────────────────────────────────────
async def ensure_permissions(engine_pyp: AsyncEngine, codes: dict[str, str] | None = None) -> None:
    """确保权限目录入库（ON CONFLICT DO NOTHING）。缺省播种整张 PERMISSIONS。"""
    catalog = codes or PERMISSIONS
    async with engine_pyp.begin() as conn:
        for code, desc in catalog.items():
            await conn.execute(
                pg_insert(Permission.__table__)
                .values(code=code, description=desc)
                .on_conflict_do_nothing(index_elements=["code"])
            )


async def ensure_role(engine_pyp: AsyncEngine, name: str, description: str | None = None) -> int:
    """确保角色存在；返回 role_id。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(Role.__table__)
            .values(name=name, description=description)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        return int((await conn.execute(select(Role.id).where(Role.name == name))).scalar_one())


async def grant_role_permissions(engine_pyp: AsyncEngine, role_name: str, codes: list[str]) -> None:
    """给角色授一组权限码（幂等）。"""
    async with engine_pyp.begin() as conn:
        role_id = (await conn.execute(select(Role.id).where(Role.name == role_name))).scalar_one()
        for code in codes:
            pid = (await conn.execute(select(Permission.id).where(Permission.code == code))).scalar()
            if pid is not None:
                await conn.execute(
                    pg_insert(RolePermission.__table__)
                    .values(role_id=role_id, permission_id=pid)
                    .on_conflict_do_nothing(index_elements=["role_id", "permission_id"])
                )


async def assign_role(engine_pyp: AsyncEngine, user_id: int, role_name: str) -> None:
    """把角色赋予用户（幂等）。"""
    async with engine_pyp.begin() as conn:
        role_id = (await conn.execute(select(Role.id).where(Role.name == role_name))).scalar_one()
        await conn.execute(
            pg_insert(UserRole.__table__)
            .values(user_id=user_id, role_id=role_id)
            .on_conflict_do_nothing(index_elements=["user_id", "role_id"])
        )


async def seed_default_rbac(engine_pyp: AsyncEngine) -> None:
    """播种权限目录 + 默认四角色及其权限矩阵（幂等，可反复跑）。不动用户分配。"""
    await ensure_permissions(engine_pyp)
    for role_name, codes in DEFAULT_ROLES.items():
        await ensure_role(engine_pyp, role_name)
        await grant_role_permissions(engine_pyp, role_name, codes)


async def make_superuser(engine_pyp: AsyncEngine, username: str) -> bool:
    """把某用户设为超级用户（赋 管理员 角色）。用户不存在返回 False。播种默认 RBAC 后调用。"""
    async with engine_pyp.connect() as conn:
        uid = (await conn.execute(select(User.id).where(User.username == username))).scalar()
    if uid is None:
        return False
    await assign_role(engine_pyp, int(uid), "管理员")
    return True


__all__ = [
    "DEFAULT_ROLES",
    "PERMISSIONS",
    "WILDCARD",
    "assign_role",
    "effective_permissions",
    "ensure_permissions",
    "ensure_role",
    "grant_role_permissions",
    "has_permission",
    "make_superuser",
    "seed_default_rbac",
]
