"""data_center 动态表管理 + 分流入库（Ingestor）。

`data_{source短码}` 是**运行时动态表**（加法演进，不进 Alembic）：固定系统列 + 用户字段 JSONB
+ 勾选「需索引」字段提升为 STORED 生成列 + B-tree。入库用数据指纹 `ON CONFLICT DO UPDATE` 幂等
（刷 updated_at）——去重是设计目标。系统列见 SDD §4.2 / 02 §定案-4。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence

from payipa_contracts import Channel, Item
from sqlalchemy import (
    BigInteger,
    Column,
    Computed,
    DateTime,
    Index,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.ident import check_code, check_field


def data_table_name(source_uuid: str, channel: Channel | str = Channel.PROD) -> str:
    """返回按任务通道物理隔离的数据表名。"""
    value = Channel(channel)
    prefix = "test_data" if value is Channel.TEST else "data"
    return f"{prefix}_{check_code(source_uuid)}"


def build_data_table(
    source_uuid: str,
    indexed_fields: Sequence[str] = (),
    channel: Channel | str = Channel.PROD,
) -> Table:
    """构造一个数据源的混合 schema 表对象（用独立 MetaData，不污染 Alembic 元数据）。

    勾选索引的字段 -> ``idx_<field>`` STORED 生成列（``fields ->> 'field'``）+ B-tree。
    """
    md = MetaData()  # 每次独立，避免与 alembic 元数据/其它表名冲突
    name = data_table_name(source_uuid, channel)
    columns: list[Column] = [
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("data_fingerprint", String(64), nullable=False, unique=True),  # 表内/源内唯一
        Column("batch_id", BigInteger, nullable=True),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column(
            "updated_at",
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        Column("state", SmallInteger, nullable=False, server_default="3"),  # 正=正常态、负=错误码
        Column("owner_id", BigInteger, nullable=True),
        Column("tenant_id", BigInteger, nullable=True),  # RLS 预留
        Column("fields", JSONB, nullable=False, server_default="{}"),  # 用户字段袋
        Column("field_meta", JSONB, nullable=False, server_default="{}"),  # 每字段证据链
    ]
    for f in indexed_fields:
        check_field(f)  # 字段名内插进生成列表达式/索引名，先过统一校验（P0-13）
        columns.append(Column(f"idx_{f}", Text, Computed(f"(fields ->> '{f}')", persisted=True)))
    table = Table(name, md, *columns)
    for f in indexed_fields:
        Index(f"ix_{name}_idx_{f}", table.c[f"idx_{f}"])
    return table


async def create_data_table(engine: AsyncEngine, table: Table) -> None:
    """建表（幂等 checkfirst）。data_* 由 core 在建源时程序化 DDL，不进 Alembic。"""
    async with engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all, checkfirst=True)


async def drop_data_table(engine: AsyncEngine, table: Table) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(table.metadata.drop_all, checkfirst=True)


def compute_data_fingerprint(fields: dict, fingerprint_keys: Sequence[str] = ()) -> str:
    """数据指纹 = 业务字段组合排序 md5。未配置指纹字段时退化为全字段排序。"""
    payload = {k: fields.get(k) for k in sorted(fingerprint_keys)} if fingerprint_keys else dict(sorted(fields.items()))
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(blob.encode("utf-8")).hexdigest()  # noqa: S324  指纹用途非安全


class Ingestor:
    """分流入库：结构化 Item upsert 进 data_{source} 表（指纹幂等）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def upsert(
        self,
        table: Table,
        items: Iterable[Item],
        *,
        batch_id: int | None = None,
        fingerprint_keys: Sequence[str] = (),
    ) -> int:
        """按数据指纹 upsert；返回写入行数。跨库一致性：先写数据、后置状态（调用方保证顺序）。"""
        rows = [
            {
                "data_fingerprint": compute_data_fingerprint(item.fields, fingerprint_keys),
                "batch_id": batch_id,
                "state": 3,
                "fields": item.fields,
                "field_meta": {k: v.model_dump(mode="json") for k, v in item.field_meta.items()},
            }
            for item in items
        ]
        if not rows:
            return 0
        stmt = pg_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["data_fingerprint"],
            set_={
                "fields": stmt.excluded.fields,
                "field_meta": stmt.excluded.field_meta,
                "batch_id": stmt.excluded.batch_id,
                "state": stmt.excluded.state,
                "updated_at": func.now(),
            },
        )
        async with self.engine.begin() as conn:
            await conn.execute(stmt)
        return len(rows)
