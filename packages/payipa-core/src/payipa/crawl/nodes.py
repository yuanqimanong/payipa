"""持久化节点注册表：一次性入网、长期凭证、心跳与离线状态。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from payipa.db.pyp import Agent, AgentEnrollment
from payipa.security.tokens import new_enrollment_token


async def issue_agent_enrollment(
    engine_pyp: AsyncEngine, *, created_by: int | None, ttl_s: int = 600
) -> tuple[str, datetime]:
    """创建一次性入网码，返回（明文、过期时间）；明文不会写库。"""
    if not 60 <= ttl_s <= 3600:
        raise ValueError("入网码有效期须在 60–3600 秒之间")
    plain, token_hash = new_enrollment_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_s)
    async with engine_pyp.begin() as conn:
        await conn.execute(
            AgentEnrollment.__table__.insert().values(
                token_hash=token_hash,
                expires_at=expires_at,
                created_by=created_by,
            )
        )
    return plain, expires_at


async def _register_agent_conn(
    conn: AsyncConnection,
    agent_id: str,
    *,
    hostname: str,
    slot_n: int,
    capabilities: dict,
    node_token_hash: str | None,
) -> tuple[int, str | None]:
    """在调用方事务内注册；已有长期凭证绝不允许被新注册覆盖。"""
    existing = (
        await conn.execute(select(Agent.node_token_hash).where(Agent.agent_id == agent_id).with_for_update())
    ).first()
    if existing is not None and node_token_hash is not None and existing.node_token_hash not in (None, node_token_hash):
        raise PermissionError("agent_id 已绑定其他节点凭证；请先在主控撤销该节点")

    values: dict = {
        "agent_id": agent_id,
        "hostname": hostname,
        "slot_n": slot_n,
        "capabilities": capabilities,
        "status": "online",
        "last_heartbeat": func.now(),
    }
    updates = {k: v for k, v in values.items() if k != "agent_id"}
    if node_token_hash is not None:
        values["node_token_hash"] = node_token_hash
        updates["node_token_hash"] = node_token_hash
    await conn.execute(
        pg_insert(Agent.__table__).values(**values).on_conflict_do_update(index_elements=["agent_id"], set_=updates)
    )
    row = (await conn.execute(select(Agent.weight, Agent.group_name).where(Agent.agent_id == agent_id))).first()
    return (row[0] if row else 1), (row[1] if row else None)


async def register_agent(
    engine_pyp: AsyncEngine,
    agent_id: str,
    *,
    hostname: str,
    slot_n: int,
    capabilities: dict,
    node_token_hash: str | None = None,
) -> tuple[int, str | None]:
    """注册/重连时 upsert agents 行（status=online、刷新 last_heartbeat/能力/槽位）。

    node_token_hash 仅在**新签发凭证**时传入并覆盖；重连（凭 node_token 认证）传 None，
    不得清掉既有凭证 hash（P0-07：凭证生命周期闭环）。
    返回 (weight, group_name)——由管理员在库中预置，回灌 hub 用于加权/分组派发；新节点默认 weight=1。
    """
    async with engine_pyp.begin() as conn:
        return await _register_agent_conn(
            conn,
            agent_id,
            hostname=hostname,
            slot_n=slot_n,
            capabilities=capabilities,
            node_token_hash=node_token_hash,
        )


async def enroll_agent(
    engine_pyp: AsyncEngine,
    enrollment_hash: str,
    agent_id: str,
    *,
    hostname: str,
    slot_n: int,
    capabilities: dict,
    node_token_hash: str,
) -> tuple[int, str | None] | None:
    """原子消费一次性入网码并绑定节点；过期、已用或同名凭证冲突均返回 None。"""
    async with engine_pyp.begin() as conn:
        enrollment = (
            await conn.execute(
                select(AgentEnrollment.id)
                .where(
                    AgentEnrollment.token_hash == enrollment_hash,
                    AgentEnrollment.used_at.is_(None),
                    AgentEnrollment.expires_at > func.now(),
                )
                .with_for_update()
            )
        ).first()
        if enrollment is None:
            return None
        try:
            registered = await _register_agent_conn(
                conn,
                agent_id,
                hostname=hostname,
                slot_n=slot_n,
                capabilities=capabilities,
                node_token_hash=node_token_hash,
            )
        except PermissionError:
            return None
        await conn.execute(
            update(AgentEnrollment.__table__)
            .where(AgentEnrollment.id == enrollment.id)
            .values(used_at=func.now(), agent_id=agent_id)
        )
        return registered


async def revoke_agent_credential(engine_pyp: AsyncEngine, agent_id: str) -> bool:
    """撤销长期节点凭证并标离线；节点须用新的单次入网码重新接入。"""
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Agent.__table__).where(Agent.agent_id == agent_id).values(node_token_hash=None, status="offline")
        )
    return bool(res.rowcount)


async def auth_node(engine_pyp: AsyncEngine, token_hash: str) -> str | None:
    """按长期节点凭证 hash 找回 agent_id（重连认证，P0-07）；无匹配返回 None。"""
    async with engine_pyp.connect() as conn:
        return (await conn.execute(select(Agent.agent_id).where(Agent.node_token_hash == token_hash).limit(1))).scalar()


async def touch_agent(engine_pyp: AsyncEngine, agent_id: str) -> None:
    """心跳落库：刷新 last_heartbeat（供后续 liveness reaper/监控）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Agent.__table__).where(Agent.agent_id == agent_id).values(last_heartbeat=func.now()))


async def set_agent_offline(engine_pyp: AsyncEngine, agent_id: str) -> None:
    """断连落库：status=offline（不删行，保留历史/权重/分组配置）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Agent.__table__).where(Agent.agent_id == agent_id).values(status="offline"))
