"""M4 slice-1 集成测试（需 PG）：outbox 幂等入队 + Consumer 排空(成功/退避重试/死信) + 租约回收。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from payipa.db.pyp import PushComponent, PushOutbox
from payipa.db.settings import get_settings
from payipa.deliver import outbox
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_NAME = "m4push"


async def _state(pyp, oid: int) -> tuple[str, int]:
    async with pyp.begin() as conn:
        r = (await conn.execute(select(PushOutbox.state, PushOutbox.attempts).where(PushOutbox.id == oid))).first()
    return r.state, r.attempts


async def _force_state(pyp, oid: int, state: str) -> None:
    async with pyp.begin() as conn:
        await conn.execute(text("UPDATE push_outbox SET state=:s WHERE id=:i"), {"s": state, "i": oid})


def test_outbox_state_machine(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                cid = (
                    await conn.execute(
                        pg_insert(PushComponent.__table__).values(name=_NAME).returning(PushComponent.id)
                    )
                ).scalar_one()

            # 1) 幂等入队：同 idempotency_key 只入一次
            assert await outbox.enqueue_push(pyp, component_id=cid, idempotency_key="k1", payload_ref="p1") == 1
            assert await outbox.enqueue_push(pyp, component_id=cid, idempotency_key="k1", payload_ref="p1") == 0

            # 2) 排空成功：deliver 不抛 → sent
            delivered: list[str] = []

            async def ok(row: dict) -> None:
                delivered.append(row["idempotency_key"])

            sent, failed = await outbox.run_outbox_once(pyp, ok)
            assert (sent, failed) == (1, 0) and delivered == ["k1"]
            async with pyp.begin() as conn:
                oid1 = (
                    await conn.execute(select(PushOutbox.id).where(PushOutbox.idempotency_key == "k1"))
                ).scalar_one()
            assert (await _state(pyp, oid1))[0] == "sent"

            # 3) 排空失败 → 退避重试（attempts=1, pending, next_retry_at 未来）；未达上限不进死信
            await outbox.enqueue_push(pyp, component_id=cid, idempotency_key="k2", payload_ref="p2")

            async def boom(row: dict) -> None:
                raise RuntimeError("target down")

            sent, failed = await outbox.run_outbox_once(pyp, boom, max_attempts=3)
            assert (sent, failed) == (0, 1)
            async with pyp.begin() as conn:
                r = (
                    await conn.execute(
                        select(
                            PushOutbox.state, PushOutbox.attempts, PushOutbox.next_retry_at, PushOutbox.last_error
                        ).where(PushOutbox.idempotency_key == "k2")
                    )
                ).first()
            assert (
                r.state == "pending"
                and r.attempts == 1
                and r.next_retry_at is not None
                and "target down" in r.last_error
            )
            oid2 = None
            async with pyp.begin() as conn:
                oid2 = (
                    await conn.execute(select(PushOutbox.id).where(PushOutbox.idempotency_key == "k2"))
                ).scalar_one()

            # 退避后 next_retry_at 在未来 → 本轮不再被领取
            sent2, failed2 = await outbox.run_outbox_once(pyp, boom, max_attempts=3)
            assert (sent2, failed2) == (0, 0)

            # 4) 达上限 → 死信。mark_* 有状态守卫（仅 inflight 可终结），先把行置回 inflight 再失败
            await _force_state(pyp, oid2, "inflight")
            await outbox.mark_failed(pyp, oid2, error="again", attempts=1, max_attempts=3)  # →2, pending
            await _force_state(pyp, oid2, "inflight")
            st = await outbox.mark_failed(pyp, oid2, error="final", attempts=2, max_attempts=3)  # →3 ≥ max → dead
            assert st == "dead" and (await _state(pyp, oid2))[0] == "dead"

            # 4b) 状态守卫：非 inflight 行的迟到终结是 no-op（租约被回收重领后旧 Consumer 不能覆盖）
            await outbox.mark_sent(pyp, oid2)
            assert (await _state(pyp, oid2))[0] == "dead"

            # 5) 租约回收：造一条 inflight + 过期租约 → requeue_expired → pending
            async with pyp.begin() as conn:
                oid3 = (
                    await conn.execute(
                        pg_insert(PushOutbox.__table__)
                        .values(
                            component_id=cid,
                            state="inflight",
                            attempts=0,
                            lease_until=datetime.now(UTC) - timedelta(seconds=10),
                        )
                        .returning(PushOutbox.id)
                    )
                ).scalar_one()
            assert await outbox.requeue_expired(pyp) >= 1
            assert (await _state(pyp, oid3))[0] == "pending"
        finally:
            async with pyp.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM push_outbox WHERE component_id IN (SELECT id FROM push_components WHERE name=:n)"
                    ),
                    {"n": _NAME},
                )
                await conn.execute(text("DELETE FROM push_components WHERE name=:n"), {"n": _NAME})
            await pyp.dispose()

    asyncio.run(main())


def test_outbox_claim_no_double(require_pg: None) -> None:
    """DB-009 验收：两个 Consumer 并发领取，领到的集合不相交、合计不超待投递总数。"""
    name = "m4claimrace"

    async def main() -> None:
        pyp_a = create_async_engine(get_settings().async_url("pyp"))
        pyp_b = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp_a.begin() as conn:
                cid = (
                    await conn.execute(pg_insert(PushComponent.__table__).values(name=name).returning(PushComponent.id))
                ).scalar_one()
            for i in range(20):
                await outbox.enqueue_push(pyp_a, component_id=cid, idempotency_key=f"race-{i}")

            got_a, got_b = await asyncio.gather(outbox.claim_due(pyp_a, limit=50), outbox.claim_due(pyp_b, limit=50))
            ids_a = {r["id"] for r in got_a if r["component_id"] == cid}
            ids_b = {r["id"] for r in got_b if r["component_id"] == cid}
            assert not (ids_a & ids_b), f"并发领取出现重复: {ids_a & ids_b}"
            assert len(ids_a) + len(ids_b) == 20
        finally:
            async with pyp_a.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM push_outbox WHERE component_id IN (SELECT id FROM push_components WHERE name=:n)"
                    ),
                    {"n": name},
                )
                await conn.execute(text("DELETE FROM push_components WHERE name=:n"), {"n": name})
            await pyp_a.dispose()
            await pyp_b.dispose()

    asyncio.run(main())
