"""CSRF 双提交防护单测（无需 PG，CI 也跑）：登录表单缺/错 token → 403；GET 下发 cookie。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pyp_server.main import create_app

client = TestClient(create_app())


def test_login_get_issues_csrf_cookie_and_field() -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert client.cookies.get("pyp_csrf")
    assert 'name="csrf_token"' in resp.text


def test_login_post_without_token_rejected() -> None:
    c = TestClient(create_app())
    assert c.post("/login", data={"username": "x", "password": "y"}, follow_redirects=False).status_code == 403


def test_login_post_with_mismatched_token_rejected() -> None:
    c = TestClient(create_app())
    c.get("/login")  # 设 cookie
    resp = c.post(
        "/login", data={"username": "x", "password": "y", "csrf_token": "not-the-cookie"}, follow_redirects=False
    )
    assert resp.status_code == 403
