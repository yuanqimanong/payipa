"""M1-5：查看页（SSR，无 DB）+ /api/data 端点（空表返回空，需 PG）。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pyp_server.main import app

client = TestClient(app)


def test_data_page_renders_html() -> None:
    r = client.get("/data/mysource")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "mysource" in r.text  # 模板注入了 source
    assert "tabulator.min.js" in r.text  # 引用了 vendored Tabulator（非 CDN）


def test_vendored_static_served() -> None:
    for path in (
        "/static/vendor/tabulator/tabulator.min.js",
        "/static/vendor/tabulator/tabulator.min.css",
        "/static/vendor/htmx/htmx.min.js",
    ):
        assert client.get(path).status_code == 200, path


def test_data_api_missing_table_returns_empty(require_pg: None) -> None:
    r = client.get("/api/data/nope_no_such_source_xyz")
    assert r.status_code == 200
    assert r.json() == {"last_page": 1, "data": [], "total": 0}
