"""续爬 frontier：继承父请求规则、深度限制与批内 URL 去重。"""

from __future__ import annotations

from collections.abc import Sequence

from payipa_contracts import (
    RequestState,
    RulePack,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from payipa.crawl._policy import url_fingerprint
from payipa.db.pyp import Batch, Request, Rule, Source, Task


async def _enqueue_discovered_conn(conn: AsyncConnection, parent_req_id: int, urls: Sequence[str]) -> int:
    """在现有 pyp 事务内派生续爬请求；结果提交会在持有父请求行锁时调用。"""
    if not urls:
        return 0
    parent = (
        await conn.execute(
            select(
                Request.batch_id,
                Request.depth,
                Request.rule_id,
                Request.rule_hash,
                Request.rule_version,
                Batch.status,
                Source.access_confirmed_at,
                Source.paused_at,
            )
            .select_from(Request.__table__)
            .join(Batch.__table__, Request.batch_id == Batch.id)
            .join(Task.__table__, Batch.task_id == Task.id)
            .join(Source.__table__, Task.source_id == Source.id)
            .where(Request.id == parent_req_id)
        )
    ).first()
    if parent is None:
        return 0
    batch_id, parent_depth, rule_id, rule_hash, rule_version, batch_status, confirmed_at, paused_at = parent
    if batch_status != "running" or confirmed_at is None or paused_at is not None:
        return 0
    child_depth = (parent_depth or 0) + 1
    spec = (await conn.execute(select(Rule.spec).where(Rule.id == rule_id))).scalar() if rule_id else None
    pack = RulePack.model_validate(spec) if spec else None
    max_depth = pack.crawl.max_depth if (pack and pack.crawl) else 0
    if child_depth > max_depth:
        return 0
    inserted = 0
    seen: set[str] = set()
    for url in urls:
        uh = url_fingerprint(url)
        if uh in seen:
            continue
        seen.add(uh)
        res = await conn.execute(
            pg_insert(Request.__table__)
            .values(
                batch_id=batch_id,
                target=url,
                rule_id=rule_id,
                rule_hash=rule_hash,
                rule_version=rule_version,
                state=int(RequestState.QUEUED),
                depth=child_depth,
                url_hash=uh,
            )
            .on_conflict_do_nothing(index_elements=["batch_id", "url_hash"])
        )
        inserted += res.rowcount or 0
    return inserted


async def enqueue_discovered(engine_pyp: AsyncEngine, parent_req_id: int, urls: Sequence[str]) -> int:
    """多波爬行：把父请求本页发现的链接并入**同一批次**入队。

    URL 指纹批内去重（唯一索引 (batch_id, url_hash) + ON CONFLICT DO NOTHING）；depth=父+1；
    仅当 depth ≤ rule.crawl.max_depth 才入队（无 crawl 规则 ⇒ max_depth=0 ⇒ 不跟进，退化为单页）。
    返回实际新入队条数。**须在批次收尾判定前调用**，新 QUEUED 落库以防跨波提前 finalize。
    """
    async with engine_pyp.begin() as conn:
        return await _enqueue_discovered_conn(conn, parent_req_id, urls)
