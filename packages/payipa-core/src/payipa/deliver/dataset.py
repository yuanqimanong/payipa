"""对外 Dataset API（M4 slice-2）：把已发布的组装产物 asm_{短码} 作只读分页数据集对外开放。

响应 = JSON 行 + keyset next_cursor（对外 JSON，区别于内部 Arrow IPC）。API Key 鉴权 + scope.datasets 白名单授权。
数据集内容来自 business 库的 asm_{短码}（组装产物）。跨库不 join；只读。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import ApiKey
from payipa.security.api_key import hash_api_key, new_api_key
from payipa.studio.asm import build_asm_table


async def create_api_key(engine_pyp: AsyncEngine, *, name: str, datasets: list[str], quota: int | None = None) -> str:
    """签发一个 API Key（存 hash + scope）；返回明文（只此一次）。scope.datasets = 可读产物短码白名单。"""
    plain, digest = new_api_key()
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(ApiKey.__table__).values(
                name=name, key_hash=digest, scope={"datasets": list(datasets)}, quota=quota, revoked=False
            )
        )
    return plain


async def verify_api_key(engine_pyp: AsyncEngine, plain: str) -> dict | None:
    """校验对外 API Key：hash 查库、未吊销则返回 scope，否则 None。"""
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(select(ApiKey.scope, ApiKey.revoked).where(ApiKey.key_hash == hash_api_key(plain)))
        ).first()
    if row is None or row.revoked:
        return None
    return row.scope or {}


def api_key_allows_dataset(scope: dict, product_code: str) -> bool:
    return product_code in (scope or {}).get("datasets", [])


async def read_dataset(
    engine_business: AsyncEngine, product_code: str, *, after_id: int = 0, limit: int = 100
) -> tuple[list[dict], int | None]:
    """读组装产物 asm_{product_code} 的一页（id 升序 keyset）；返回 (行[{id, ...fields}], 下一页 after_id|None)。"""
    table = build_asm_table(product_code)
    stmt = (
        select(table.c["id"], table.c["fields"], table.c["created_at"])
        .where(table.c["id"] > after_id)
        .order_by(table.c["id"].asc())
        .limit(limit + 1)
    )
    async with engine_business.connect() as conn:
        fetched = (await conn.execute(stmt)).mappings().all()
    has_more = len(fetched) > limit
    page = fetched[:limit]
    rows = [{"id": r["id"], "created_at": r["created_at"], **(r["fields"] or {})} for r in page]
    next_after = page[-1]["id"] if (has_more and page) else None
    return rows, next_after
