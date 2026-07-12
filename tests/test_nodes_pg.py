"""M2-B 节点注册表集成测试（需 PG）：register_agent upsert + 回灌 weight/group + touch + offline。"""

from __future__ import annotations

import asyncio

import pytest
from payipa.crawl import run
from payipa.db.pyp import Agent
from payipa.db.settings import get_settings
from payipa.security.tokens import hash_token, new_node_token
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

_AID = "test-node-m2b"


def test_register_agent_lifecycle(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            t1, h1 = new_node_token()
            assert t1 and h1 and t1 != h1 and len(h1) == 64  # 明文≠hash、sha256 十六进制 64 位

            # 首次注册：默认 weight=1、group=None、status=online、token hash 落库
            weight, group = await run.register_agent(
                pyp, _AID, hostname="hostA", slot_n=4, capabilities={"automation": False}, node_token_hash=h1
            )
            assert weight == 1 and group is None
            async with pyp.begin() as conn:
                row = (
                    await conn.execute(
                        select(Agent.status, Agent.slot_n, Agent.node_token_hash, Agent.last_heartbeat).where(
                            Agent.agent_id == _AID
                        )
                    )
                ).first()
            assert row.status == "online" and row.slot_n == 4 and row.node_token_hash == h1 and row.last_heartbeat

            # 管理员预置 weight/group 后，任何新注册都不得覆盖已有长期凭证。
            async with pyp.begin() as conn:
                await conn.execute(
                    update(Agent.__table__).where(Agent.agent_id == _AID).values(weight=7, group_name="automation")
                )
            t2, h2 = new_node_token()
            with pytest.raises(PermissionError):
                await run.register_agent(
                    pyp, _AID, hostname="hostA2", slot_n=8, capabilities={"automation": True}, node_token_hash=h2
                )

            # 凭证重连（node_token_hash=None）：回灌权重/分组、刷新能力，但保留原凭证 hash。
            weight2, group2 = await run.register_agent(
                pyp, _AID, hostname="hostA3", slot_n=8, capabilities={"automation": True}
            )
            assert weight2 == 7 and group2 == "automation"
            async with pyp.begin() as conn:
                cnt = (
                    await conn.execute(
                        select(text("count(*)")).select_from(Agent.__table__).where(Agent.agent_id == _AID)
                    )
                ).scalar()
                row = (
                    await conn.execute(
                        select(Agent.slot_n, Agent.hostname, Agent.node_token_hash).where(Agent.agent_id == _AID)
                    )
                ).first()
            assert cnt == 1 and row.slot_n == 8 and row.hostname == "hostA3" and row.node_token_hash == h1

            # 凭证认证：hash 命中回 agent_id；不命中回 None
            assert await run.auth_node(pyp, h1) == _AID
            assert await run.auth_node(pyp, "0" * 64) is None

            # 一次性入网码无法劫持已有 id；显式撤销后可原子消费并重新绑定，且不能复用。
            enrollment, expires_at = await run.issue_agent_enrollment(pyp, created_by=None, ttl_s=600)
            assert enrollment.startswith("pyp_enroll_") and expires_at
            assert (
                await run.enroll_agent(
                    pyp,
                    hash_token(enrollment),
                    _AID,
                    hostname="hijack",
                    slot_n=1,
                    capabilities={},
                    node_token_hash=h2,
                )
                is None
            )
            assert await run.revoke_agent_credential(pyp, _AID) is True
            assert await run.auth_node(pyp, h1) is None
            assert await run.enroll_agent(
                pyp,
                hash_token(enrollment),
                _AID,
                hostname="hostB",
                slot_n=6,
                capabilities={"automation": True},
                node_token_hash=h2,
            ) == (7, "automation")
            assert await run.auth_node(pyp, h2) == _AID
            assert (
                await run.enroll_agent(
                    pyp,
                    hash_token(enrollment),
                    "other-agent",
                    hostname="other",
                    slot_n=1,
                    capabilities={},
                    node_token_hash="f" * 64,
                )
                is None
            )

            # touch 刷新 last_heartbeat（新值 ≥ 旧值）
            async with pyp.begin() as conn:
                before = (await conn.execute(select(Agent.last_heartbeat).where(Agent.agent_id == _AID))).scalar()
            await run.touch_agent(pyp, _AID)
            async with pyp.begin() as conn:
                after = (await conn.execute(select(Agent.last_heartbeat).where(Agent.agent_id == _AID))).scalar()
            assert after >= before

            # offline：status=offline，保留权重/分组配置
            await run.set_agent_offline(pyp, _AID)
            async with pyp.begin() as conn:
                row = (
                    await conn.execute(
                        select(Agent.status, Agent.weight, Agent.group_name).where(Agent.agent_id == _AID)
                    )
                ).first()
            assert row.status == "offline" and row.weight == 7 and row.group_name == "automation"
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM agent_enrollments WHERE agent_id=:a"), {"a": _AID})
                await conn.execute(text("DELETE FROM agents WHERE agent_id=:a"), {"a": _AID})
            await pyp.dispose()

    asyncio.run(main())


def test_singleton_lock(require_pg: None) -> None:
    """P0-09：单实例 advisory lock——第二个连接拿不到；释放后可再拿。"""

    async def main() -> None:
        from pyp_server.runtime import try_lock, unlock

        e1 = create_async_engine(get_settings().async_url("pyp"))
        e2 = create_async_engine(get_settings().async_url("pyp"))
        try:
            c1 = await e1.connect()
            assert await try_lock(c1) is True
            async with e2.connect() as c2:
                assert await try_lock(c2) is False  # 第二实例拒绝
            # close 只把连接还回池子（会话未断，锁仍持有）——必须显式 unlock
            await unlock(c1)
            await c1.close()
            async with e2.connect() as c3:
                assert await try_lock(c3) is True
                await unlock(c3)
        finally:
            await e1.dispose()
            await e2.dispose()

    asyncio.run(main())
