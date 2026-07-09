"""M2-B 节点注册表集成测试（需 PG）：register_agent upsert + 回灌 weight/group + touch + offline。"""

from __future__ import annotations

import asyncio

from payipa.crawl import run
from payipa.db.pyp import Agent
from payipa.db.settings import get_settings
from payipa.security.tokens import new_node_token
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

            # 管理员预置 weight/group 后重连（upsert）：应回灌新值，且不新增行
            async with pyp.begin() as conn:
                await conn.execute(
                    update(Agent.__table__).where(Agent.agent_id == _AID).values(weight=7, group_name="automation")
                )
            t2, h2 = new_node_token()
            weight2, group2 = await run.register_agent(
                pyp, _AID, hostname="hostA2", slot_n=8, capabilities={"automation": True}, node_token_hash=h2
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
            assert cnt == 1 and row.slot_n == 8 and row.hostname == "hostA2" and row.node_token_hash == h2

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
                await conn.execute(text("DELETE FROM agents WHERE agent_id=:a"), {"a": _AID})
            await pyp.dispose()

    asyncio.run(main())
