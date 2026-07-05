"""M1-2 集成测试（需 PG）：/internal/upload token 鉴权 + zstd 存 local + 登记 artifacts。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_internal_upload_roundtrip(require_pg: None, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("UPLOAD_SECRET", "testsecret")

    from payipa import storage as sto
    from payipa.db import settings as st

    st.get_settings.cache_clear()
    sto.get_storage.cache_clear()
    try:
        from payipa.security.tokens import issue_upload_token
        from pyp_server.main import create_app

        app = create_app()
        raw = b"<html>book</html>" * 50
        with TestClient(app) as client:
            token = issue_upload_token("testsecret", "srcX", 3, ttl_s=60)
            ok = client.post(
                "/internal/upload",
                params={
                    "source_uuid": "srcX",
                    "batch_id": 3,
                    "url": "https://books.example.com/b1",
                    "content_type": "text/html",
                    "task_id": "t1",
                },
                headers={"x-upload-token": token},
                content=raw,
            )
            assert ok.status_code == 200, ok.text
            ref = ok.json()
            assert ref["backend"] == "local"
            assert ref["object_key"].startswith("srcX/raw/3/")
            assert ref["object_key"].endswith(".zst")
            assert (tmp_path / ref["object_key"]).exists()

            # 坏 token → 401
            bad = client.post(
                "/internal/upload",
                params={"source_uuid": "srcX", "batch_id": 3, "url": "https://books.example.com/b1"},
                headers={"x-upload-token": "nope.sig"},
                content=raw,
            )
            assert bad.status_code == 401
    finally:
        st.get_settings.cache_clear()
        sto.get_storage.cache_clear()
