"""M1-2 单元测试（无 DB）：URL 指纹/key 方案、LocalBackend zstd 存取、上传 token。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from payipa.security.tokens import issue_upload_token, verify_upload_token
from payipa.storage.keys import canonicalize_url, raw_object_key, url_fingerprint
from payipa.storage.local import LocalBackend


def test_canonicalize_url_sorts_query_and_drops_fragment() -> None:
    a = canonicalize_url("https://X.com/p?b=2&a=1#frag")
    b = canonicalize_url("https://x.com/p?a=1&b=2")
    assert a == b
    assert "#frag" not in a


def test_url_fingerprint_query_order_invariant() -> None:
    assert url_fingerprint("https://x.com?a=1&b=2") == url_fingerprint("https://x.com?b=2&a=1")


def test_raw_object_key_scheme() -> None:
    key = raw_object_key("abc", 7, "https://x.com/p")
    assert key.startswith("abc/raw/7/")
    assert key.endswith(".zst")


def test_local_backend_zstd_roundtrip(tmp_path: Path) -> None:
    async def main() -> None:
        backend = LocalBackend(tmp_path)
        assert backend.disk_ok()
        raw = b"<html>hello</html>" * 200
        ref = await backend.save_raw("src1", 1, "https://x.com/a", raw, content_type="text/html")
        assert ref.backend.value == "local"
        assert ref.object_key.endswith(".zst")
        stored = tmp_path / ref.object_key
        assert stored.exists()
        assert stored.stat().st_size == ref.size
        assert stored.stat().st_size < len(raw)  # 确实压缩了
        assert await backend.get_raw(ref.object_key) == raw  # 解压回原文

    asyncio.run(main())


def test_local_backend_rejects_path_traversal(tmp_path: Path) -> None:
    async def main() -> None:
        backend = LocalBackend(tmp_path)
        with pytest.raises(ValueError):
            await backend.save_bytes("../evil", b"x")

    asyncio.run(main())


def test_upload_token_roundtrip() -> None:
    token = issue_upload_token("secret", "src1", 5, ttl_s=60)
    claims = verify_upload_token("secret", token)
    assert claims is not None
    assert claims["s"] == "src1"
    assert claims["b"] == "5"
    assert verify_upload_token("wrong-secret", token) is None  # 签名不符
    assert verify_upload_token("secret", token, now=99_999_999_999) is None  # 过期
    assert verify_upload_token("secret", token + "x") is None  # 篡改
