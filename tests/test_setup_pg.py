"""首次启动引导（P0-05）：未初始化引导建管理员；已初始化则本页关闭（不构成公开建号面）。

「未初始化」= users 表为空。真库不能删他人数据，故建号流程仅在 users 确实为空时跑
（CI 全新迁移库即空），非空时跳过并只验收「已初始化则关闭」的行为。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from pyp_server.main import create_app
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

_NAME = "setup-probe-admin"


async def _user_count() -> int:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(User.__table__))).scalar()
    finally:
        await pyp.dispose()


def test_setup_gating(require_pg: None) -> None:
    """按真库状态验收：空表 → GET /setup 200、/login 跳 /setup；非空 → /setup 跳 /login。"""
    empty = asyncio.run(_user_count()) == 0
    with TestClient(create_app()) as client:
        setup = client.get("/setup", follow_redirects=False)
        login = client.get("/login", follow_redirects=False)
    if empty:
        assert setup.status_code == 200
        assert login.status_code == 303 and login.headers["location"] == "/setup"
    else:
        assert setup.status_code == 303 and setup.headers["location"] == "/login"
        assert login.status_code == 200


def test_setup_creates_first_admin(require_pg: None) -> None:
    if asyncio.run(_user_count()) != 0:
        pytest.skip("users 表非空，无法安全模拟首次启动（不删他人数据）")

    async def _cleanup() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username=:n)"),
                    {"n": _NAME},
                )
                await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _NAME})
        finally:
            await pyp.dispose()

    try:
        with TestClient(create_app()) as client:
            client.get("/setup")  # 下发 csrf cookie
            token = client.cookies.get("pyp_csrf")
            # 密码不一致 → 回显错误、不建号
            r = client.post(
                "/setup",
                data={
                    "username": _NAME,
                    "password": "abcd1234",
                    "password2": "X",
                    "bootstrap_token": "dev",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert r.status_code == 400 and "不一致" in r.text

            # 正确提交 → 建号 + 管理员角色 → 跳登录；建号后本页关闭
            r = client.post(
                "/setup",
                data={
                    "username": _NAME,
                    "password": "abcd1234",
                    "password2": "abcd1234",
                    "bootstrap_token": "dev",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert r.status_code == 303 and r.headers["location"] == "/login"
            assert client.get("/setup", follow_redirects=False).status_code == 303  # 已初始化 → 关闭

        async def _roles() -> list[str]:
            pyp = create_async_engine(get_settings().async_url("pyp"))
            try:
                async with pyp.connect() as conn:
                    uid = (await conn.execute(select(User.id).where(User.username == _NAME))).scalar()
                    rows = await conn.execute(
                        text("SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=:u"),
                        {"u": uid},
                    )
                    return list(rows.scalars().all())
            finally:
                await pyp.dispose()

        assert "管理员" in asyncio.run(_roles())
    finally:
        asyncio.run(_cleanup())
