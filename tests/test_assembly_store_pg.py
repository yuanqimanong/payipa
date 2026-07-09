"""M3 slice-5 集成测试（需 PG）：AssemblyStore 内容寻址去重 + 版本递增 + 发布签名 + 签名门 assert_runnable。"""

from __future__ import annotations

import asyncio

import pytest
from payipa.db.settings import get_settings
from payipa.studio.store import AssemblyStore, assert_runnable, verify_assembly_signature
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_NAME = "m3asm"
_SECRET = "assembly-sign-secret-at-least-32-bytes-xx"


def test_assembly_versioning_and_sign_gate(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            store = AssemblyStore(pyp)
            # 1) put：新内容 → draft、version=1、content_hash 定
            a1, h1, v1 = await store.put(
                name=_NAME, product_code="p1", script_ref="ref://s1", fingerprint_keys=["k"], indexed_fields=["k"]
            )
            assert v1 == 1 and h1
            # 相同内容再 put → 去重（同 id/hash/version）
            a1b, h1b, v1b = await store.put(
                name=_NAME, product_code="p1", script_ref="ref://s1", fingerprint_keys=["k"], indexed_fields=["k"]
            )
            assert (a1b, h1b, v1b) == (a1, h1, v1)
            # 内容变了（不同 script_ref）→ 新版本 version=2、不同 hash
            a2, h2, v2 = await store.put(name=_NAME, product_code="p1", script_ref="ref://s2")
            assert v2 == 2 and h2 != h1 and a2 != a1

            # 2) 签名门：draft 未发布 → 不可跑
            row = await store.get(a1)
            assert row.status == "draft"
            with pytest.raises(PermissionError):
                assert_runnable(row, _SECRET)

            # 3) 发布 → active + 签名；assert_runnable 通过；get_active 命中
            sig = await store.publish(a1, _SECRET)
            assert verify_assembly_signature(_SECRET, h1, sig)
            row2 = await store.get(a1)
            assert row2.status == "active" and row2.signature == sig
            assert_runnable(row2, _SECRET)  # 不抛
            active = await store.get_active("p1")
            assert active is not None and active.id == a1

            # 4) 签名/内容被篡改 → 校验失败、门拒绝
            assert not verify_assembly_signature(_SECRET, h1, sig[:-2] + "00")  # 改签名
            assert not verify_assembly_signature(_SECRET, "tampered-hash", sig)  # 改内容 hash
            assert not verify_assembly_signature("wrong-secret-also-32-bytes-long-xxxxxx", h1, sig)  # 换密钥
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM assemblies WHERE name=:n"), {"n": _NAME})
            await pyp.dispose()

    asyncio.run(main())
