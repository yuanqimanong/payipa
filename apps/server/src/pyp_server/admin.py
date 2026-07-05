"""管理员运维命令（`pyp-admin`）。M-登录：创建首个管理员账号（无自助注册，06 定案）。

用法：``uv run pyp-admin create-user <username> <password>``
"""

from __future__ import annotations

import argparse
import asyncio

from payipa.db.engine import get_engine
from payipa.db.pyp import User
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pyp_server.auth import hash_password


async def _create_user(username: str, password: str) -> str:
    engine = get_engine("pyp")
    async with engine.begin() as conn:
        existing = (await conn.execute(select(User.id).where(User.username == username))).first()
        if existing:
            return f"用户 '{username}' 已存在 (id={existing[0]})"
        uid = (
            await conn.execute(
                pg_insert(User.__table__)
                .values(username=username, password_hash=hash_password(password), status="active")
                .returning(User.id)
            )
        ).scalar_one()
    await engine.dispose()
    return f"已创建用户 '{username}' (id={uid})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyp-admin", description="payipa 运维命令")
    sub = parser.add_subparsers(dest="cmd")
    cu = sub.add_parser("create-user", help="创建用户（管理员开通）")
    cu.add_argument("username")
    cu.add_argument("password")
    args = parser.parse_args(argv)
    if args.cmd == "create-user":
        print(asyncio.run(_create_user(args.username, args.password)))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
