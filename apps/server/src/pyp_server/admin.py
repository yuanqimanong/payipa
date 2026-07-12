"""管理员运维命令（`pyp-admin`）。无自助注册（06 定案）+ RBAC 播种/授权（M5）。

用法：
  uv run pyp-admin create-user <username> [--superuser]              # 密码隐藏输入
  uv run pyp-admin seed-rbac                                          # 播种默认权限目录 + 四角色矩阵
  uv run pyp-admin grant-role <username> <role>                      # 给用户赋角色（如 管理员/技术/运营/运维）
  uv run pyp-admin schema-status                                     # 查看动态表 provisioning 异常
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from payipa.db.engine import get_engine
from payipa.db.pyp import Source, User
from payipa.security.rbac import DEFAULT_ROLES, assign_role, make_superuser, seed_default_rbac
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import NoResultFound

from pyp_server.auth import hash_password


async def _create_user(username: str, password: str, *, superuser: bool = False) -> str:
    engine = get_engine("pyp")
    try:
        async with engine.begin() as conn:
            existing = (await conn.execute(select(User.id).where(User.username == username))).first()
            if existing:
                uid = int(existing[0])
                msg = f"用户 '{username}' 已存在 (id={uid})"
            else:
                uid = (
                    await conn.execute(
                        pg_insert(User.__table__)
                        .values(username=username, password_hash=hash_password(password), status="active")
                        .returning(User.id)
                    )
                ).scalar_one()
                msg = f"已创建用户 '{username}' (id={uid})"
        if superuser:
            await seed_default_rbac(engine)
            await make_superuser(engine, username)
            msg += "；已赋『管理员』角色（超级用户）"
        return msg
    finally:
        await engine.dispose()


async def _seed_rbac() -> str:
    engine = get_engine("pyp")
    try:
        await seed_default_rbac(engine)
        return f"已播种权限目录 + 角色：{', '.join(DEFAULT_ROLES)}"
    finally:
        await engine.dispose()


async def _grant_role(username: str, role: str) -> str:
    engine = get_engine("pyp")
    try:
        async with engine.connect() as conn:
            uid = (await conn.execute(select(User.id).where(User.username == username))).scalar()
        if uid is None:
            return f"用户 '{username}' 不存在"
        try:
            await assign_role(engine, int(uid), role)
        except NoResultFound:
            return f"角色 '{role}' 不存在（内置角色：{', '.join(DEFAULT_ROLES)}；若尚未播种先跑 seed-rbac）"
        return f"已给 '{username}' 赋角色 '{role}'"
    finally:
        await engine.dispose()


async def _schema_status() -> tuple[int, str]:
    """Return actionable dynamic-schema issues without exposing them on the public health endpoint."""
    engine = get_engine("pyp")
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        Source.uuid,
                        Source.name,
                        Source.provisioning_state,
                        Source.provisioning_error,
                    )
                    .where(Source.provisioning_state != "ready")
                    .order_by(Source.id)
                )
            ).all()
        if not rows:
            return 0, "动态表 provisioning 全部 ready"
        lines = [f"发现 {len(rows)} 个动态表 provisioning 异常："]
        for code, name, state, error in rows:
            detail = (error or "等待后台 reconciliation")[:500]
            lines.append(f"- {code} ({name}): {state} - {detail}")
        return 2, "\n".join(lines)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyp-admin", description="payipa 运维命令")
    sub = parser.add_subparsers(dest="cmd")
    cu = sub.add_parser("create-user", help="创建用户（管理员开通）")
    cu.add_argument("username")
    cu.add_argument("--password-stdin", action="store_true", help="从标准输入读取密码（自动化用，不回显）")
    cu.add_argument("--superuser", action="store_true", help="同时播种 RBAC 并赋『管理员』角色")
    sub.add_parser("seed-rbac", help="播种默认权限目录 + 四角色矩阵")
    sub.add_parser("schema-status", help="查看动态表 provisioning 状态与错误")
    gr = sub.add_parser("grant-role", help="给用户赋角色")
    gr.add_argument("username")
    gr.add_argument("role")
    args = parser.parse_args(argv)
    if args.cmd == "create-user":
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("密码：")
        if not password:
            print("密码不能为空", file=sys.stderr)
            return 2
        print(asyncio.run(_create_user(args.username, password, superuser=args.superuser)))
        return 0
    if args.cmd == "seed-rbac":
        print(asyncio.run(_seed_rbac()))
        return 0
    if args.cmd == "grant-role":
        print(asyncio.run(_grant_role(args.username, args.role)))
        return 0
    if args.cmd == "schema-status":
        code, message = asyncio.run(_schema_status())
        print(message)
        return code
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
