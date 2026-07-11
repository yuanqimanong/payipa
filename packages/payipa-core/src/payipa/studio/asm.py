"""`business` 库组装产物动态表 `asm_{短码}` + 幂等装载（Loader）。

与 data_* 同混合 schema（复用 02 策略）：固定系统列 + 用户字段 JSONB + 勾选「需索引」→ STORED 生成列。
产物行 `state` 语义固定 = 成功（默认 3），**不与采集生命周期 state 混用**。装载按数据指纹 ON CONFLICT
幂等 upsert（写侧幂等 = 可重算三元之一）。建表程序化 DDL、不进 Alembic（M3）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

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

from payipa.crawl.ingest import compute_data_fingerprint  # 复用同一指纹方案（排序 md5）
from payipa.db.ident import check_code, check_field


def asm_table_name(product_code: str) -> str:
    return f"asm_{check_code(product_code)}"  # 短码先过统一校验（P0-13）：所有建表/取数路径都经此拼名


def build_asm_table(product_code: str, indexed_fields: Sequence[str] = ()) -> Table:
    """构造组装产物混合 schema 表（独立 MetaData，不污染 Alembic）。勾索引字段 → idx_<f> STORED + B-tree。"""
    md = MetaData()
    name = asm_table_name(product_code)
    columns: list[Column] = [
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("data_fingerprint", String(64), nullable=False, unique=True),
        Column("assembly_id", BigInteger, nullable=True),  # 产出自哪个组装任务
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
        Column("state", SmallInteger, nullable=False, server_default="3"),  # 产物态：固定成功=3
        Column("owner_id", BigInteger, nullable=True),
        Column("tenant_id", BigInteger, nullable=True),  # RLS 预留
        Column("fields", JSONB, nullable=False, server_default="{}"),  # 产物字段袋
    ]
    for f in indexed_fields:
        check_field(f)  # 字段名内插进生成列表达式/索引名，先过统一校验（P0-13）
        columns.append(Column(f"idx_{f}", Text, Computed(f"(fields ->> '{f}')", persisted=True)))
    table = Table(name, md, *columns)
    for f in indexed_fields:
        Index(f"ix_{name}_idx_{f}", table.c[f"idx_{f}"])
    return table


async def create_asm_table(engine_business: AsyncEngine, table: Table) -> None:
    """建产物表（幂等 checkfirst）。组装装载时程序化 DDL，不进 Alembic。"""
    async with engine_business.begin() as conn:
        await conn.run_sync(table.metadata.create_all, checkfirst=True)


async def drop_asm_table(engine_business: AsyncEngine, table: Table) -> None:
    async with engine_business.begin() as conn:
        await conn.run_sync(table.metadata.drop_all, checkfirst=True)


class AsmLoader:
    """把组装产出的字段行按数据指纹幂等 upsert 进 asm_{短码}。"""

    def __init__(self, engine_business: AsyncEngine) -> None:
        self.engine = engine_business

    async def upsert(
        self,
        table: Table,
        rows: Iterable[dict],
        *,
        assembly_id: int | None = None,
        fingerprint_keys: Sequence[str] = (),
    ) -> int:
        """rows = 产物字段字典列表；按指纹 ON CONFLICT DO UPDATE（幂等，刷 updated_at）。返回写入行数。"""
        payload = [
            {
                "data_fingerprint": compute_data_fingerprint(fields, fingerprint_keys),
                "assembly_id": assembly_id,
                "state": 3,
                "fields": fields,
            }
            for fields in rows
        ]
        if not payload:
            return 0
        stmt = pg_insert(table).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=["data_fingerprint"],
            set_={
                "fields": stmt.excluded.fields,
                "assembly_id": stmt.excluded.assembly_id,
                "state": stmt.excluded.state,
                "updated_at": func.now(),
            },
        )
        async with self.engine.begin() as conn:
            await conn.execute(stmt)
        return len(payload)
