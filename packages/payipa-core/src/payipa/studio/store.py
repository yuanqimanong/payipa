"""组装脚本版本化 + 签名门（M3 slice-5）。

复用 01/02 内容寻址机制：组装定义（脚本引用 + 产物配置）规范化 sha256 = content_hash（版本 pin，可重算三元之一）；
状态机 draft→testing→active；**发布即签名**（HMAC content_hash），执行器运行前校验签名（红线7：AI 产物必经
test 验证 + 签名）。测试通道试跑不落正式表由 run 层的 product_code 后缀实现。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence

from jianbing_utils import crypto
from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import Assembly

# get/get_active 返回的行视图列（经 Core 连接查显式列，非 ORM 实体）
_COLS = (
    Assembly.id,
    Assembly.status,
    Assembly.content_hash,
    Assembly.signature,
    Assembly.product_code,
    Assembly.script_ver,
)


def assembly_content_hash(
    *, script_ref: str, product_code: str, fingerprint_keys: Sequence[str], indexed_fields: Sequence[str]
) -> str:
    """组装定义规范化内容寻址：脚本引用 + 产物短码 + 指纹/索引字段（排序）→ sha256。"""
    blob = json.dumps(
        {
            "script_ref": script_ref,
            "product_code": product_code,
            "fingerprint_keys": sorted(fingerprint_keys),
            "indexed_fields": sorted(indexed_fields),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return crypto.sha256(blob)


def sign_assembly(secret: str, content_hash: str) -> str:
    """发布签名 = HMAC-SHA256(secret, content_hash) 十六进制。"""
    return hmac.new(secret.encode(), content_hash.encode(), hashlib.sha256).hexdigest()


def verify_assembly_signature(secret: str, content_hash: str, signature: str | None) -> bool:
    """校验签名与 content_hash 匹配（内容或签名被改则失败）。"""
    if not signature:
        return False
    return hmac.compare_digest(signature, sign_assembly(secret, content_hash))


def assert_runnable(assembly: Row, secret: str) -> None:
    """执行器签名门：仅 active/testing 且签名验过的组装可跑；否则抛 PermissionError。入参为 get/get_active 的行视图。"""
    if assembly.status not in ("active", "testing"):
        raise PermissionError(f"assembly {assembly.id} status={assembly.status} not runnable (need active/testing)")
    if not verify_assembly_signature(secret, assembly.content_hash or "", assembly.signature):
        raise PermissionError(f"assembly {assembly.id} signature invalid or missing")


class AssemblyStore:
    """组装定义登记（按 content_hash 去重、按 name 递增版本）+ 状态机 + 发布签名。"""

    def __init__(self, engine_pyp: AsyncEngine) -> None:
        self.engine = engine_pyp

    async def put(
        self,
        *,
        name: str,
        product_code: str,
        script_ref: str,
        fingerprint_keys: Sequence[str] = (),
        indexed_fields: Sequence[str] = (),
        trigger: str = "manual",
        upstream_task_id: int | None = None,
    ) -> tuple[int, str, int]:
        """登记组装（content_hash 去重）；返回 (assembly_id, content_hash, version)。新内容 status=draft。"""
        digest = assembly_content_hash(
            script_ref=script_ref,
            product_code=product_code,
            fingerprint_keys=fingerprint_keys,
            indexed_fields=indexed_fields,
        )
        async with self.engine.begin() as conn:
            existing = (
                await conn.execute(select(Assembly.id, Assembly.script_ver).where(Assembly.content_hash == digest))
            ).first()
            if existing:
                return int(existing[0]), digest, int(existing[1])
            max_v = (await conn.execute(select(func.max(Assembly.script_ver)).where(Assembly.name == name))).scalar()
            version = (max_v or 0) + 1
            aid = (
                await conn.execute(
                    pg_insert(Assembly.__table__)
                    .values(
                        name=name,
                        product_code=product_code,
                        script_ref=script_ref,
                        content_hash=digest,
                        status="draft",
                        script_ver=version,
                        trigger=trigger,
                        upstream_task_id=upstream_task_id,
                        fingerprint_keys=list(fingerprint_keys),
                        indexed_fields=list(indexed_fields),
                    )
                    .returning(Assembly.id)
                )
            ).scalar_one()
        return int(aid), digest, version

    async def set_status(self, assembly_id: int, status: str) -> None:
        """状态机流转（draft/testing/active）。发布(active)请用 :meth:`publish` 以同时签名。"""
        async with self.engine.begin() as conn:
            await conn.execute(update(Assembly.__table__).where(Assembly.id == assembly_id).values(status=status))

    async def publish(self, assembly_id: int, secret: str) -> str:
        """发布：status=active + 写签名（HMAC content_hash）。返回签名。"""
        async with self.engine.begin() as conn:
            ch = (await conn.execute(select(Assembly.content_hash).where(Assembly.id == assembly_id))).scalar_one()
            sig = sign_assembly(secret, ch)
            await conn.execute(
                update(Assembly.__table__).where(Assembly.id == assembly_id).values(status="active", signature=sig)
            )
        return sig

    async def get(self, assembly_id: int) -> Row | None:
        """行视图（id/status/content_hash/signature/product_code/script_ver）。"""
        async with self.engine.begin() as conn:
            return (await conn.execute(select(*_COLS).where(Assembly.id == assembly_id))).first()

    async def get_active(self, product_code: str) -> Row | None:
        """取某产物短码当前 active 的组装行视图（最高版本优先）。"""
        async with self.engine.begin() as conn:
            return (
                await conn.execute(
                    select(*_COLS)
                    .where(Assembly.product_code == product_code, Assembly.status == "active")
                    .order_by(Assembly.script_ver.desc())
                    .limit(1)
                )
            ).first()
