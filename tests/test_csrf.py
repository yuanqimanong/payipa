"""CSRF 双提交防护单测：登录表单缺/错 token → 403；GET 下发 cookie。

无 PG 时 `/login` 正常渲染登录表单；有 PG 且系统未初始化时 `/login` 会引导去 `/setup`——
两页都经 render_with_csrf 下发 pyp_csrf cookie + csrf_token 字段，故断言对两者都成立。
用 `with TestClient(...)` 单事件循环：`/login` 现会查库（首启检测），模块级 client 会跨循环炸引擎。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pyp_server.main import create_app


def test_login_get_issues_csrf_cookie_and_field() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/login")  # 未初始化时跟随 303→/setup，两页都发 csrf
        assert resp.status_code == 200
        assert client.cookies.get("pyp_csrf")
        assert 'name="csrf_token"' in resp.text


def test_login_post_without_token_rejected() -> None:
    with TestClient(create_app()) as c:
        # verify_csrf 是 login_submit 首行：无 token 直接 403（早于任何查库）
        assert c.post("/login", data={"username": "x", "password": "y"}, follow_redirects=False).status_code == 403


def test_login_post_with_mismatched_token_rejected() -> None:
    with TestClient(create_app()) as c:
        c.get("/login")  # 设 csrf cookie（无论落 /login 还是 /setup）
        resp = c.post(
            "/login", data={"username": "x", "password": "y", "csrf_token": "not-the-cookie"}, follow_redirects=False
        )
        assert resp.status_code == 403
