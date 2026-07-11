"""管理写操作 API（用户/角色维护）。JSON 端点，经 RBAC 写权限门控；会话 cookie SameSite=Lax 防跨站。

用户创建口令由平台以 argon2 哈希存储（红线9：token/密码存 hash，不落明文）。停用不删除（软删，06 定案）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from payipa.db.engine import get_engine
from payipa.db.pyp import User
from payipa.security.rbac import assign_role, revoke_role
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import NoResultFound

from pyp_server.auth import hash_password, require_perm

router = APIRouter(prefix="/api/users", tags=["manage"])


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128, description="登录用户名（唯一）")
    password: str = Field(..., min_length=8, max_length=256, description="初始密码（≥8 位；argon2 哈希存储）")
    display_name: str | None = Field(None, max_length=128, description="显示名（可选）")


class CreateUserResponse(BaseModel):
    id: int


class StatusRequest(BaseModel):
    status: Literal["active", "disabled"] = Field(..., description="active=启用 / disabled=停用（软删，不删除）")


@router.post(
    "",
    response_model=CreateUserResponse,
    summary="创建用户（管理员开通，无自助注册；密码 argon2 哈希存储）",
    dependencies=[Depends(require_perm("users.manage"))],
)
async def create_user(body: CreateUserRequest) -> CreateUserResponse:
    engine = get_engine("pyp")
    async with engine.begin() as conn:
        if (await conn.execute(select(User.id).where(User.username == body.username))).first():
            raise HTTPException(status_code=409, detail=f"用户名 {body.username!r} 已存在")
        uid = (
            await conn.execute(
                pg_insert(User.__table__)
                .values(
                    username=body.username,
                    password_hash=hash_password(body.password),
                    display_name=body.display_name,
                    status="active",
                )
                .returning(User.id)
            )
        ).scalar_one()
    return CreateUserResponse(id=int(uid))


@router.post(
    "/{user_id}/status",
    summary="启用 / 停用用户（软删，不删除）",
    dependencies=[Depends(require_perm("users.manage"))],
)
async def set_user_status(user_id: int, body: StatusRequest) -> dict:
    engine = get_engine("pyp")
    async with engine.begin() as conn:
        result = await conn.execute(update(User.__table__).where(User.id == user_id).values(status=body.status))
    if not result.rowcount:
        raise HTTPException(status_code=404, detail=f"用户 id={user_id} 不存在")
    return {"id": user_id, "status": body.status}


class RoleRequest(BaseModel):
    role: str = Field(..., min_length=1, max_length=64, description="角色名，如 管理员/技术/运营/运维")
    action: Literal["grant", "revoke"] = Field(..., description="grant=授予 / revoke=撤销")


@router.post(
    "/{user_id}/roles",
    summary="给用户授予 / 撤销角色（幂等）",
    dependencies=[Depends(require_perm("roles.manage"))],
)
async def set_user_role(user_id: int, body: RoleRequest) -> dict:
    engine = get_engine("pyp")
    async with engine.connect() as conn:
        if not (await conn.execute(select(User.id).where(User.id == user_id))).first():
            raise HTTPException(status_code=404, detail=f"用户 id={user_id} 不存在")
    try:
        if body.action == "grant":
            await assign_role(engine, user_id, body.role)
        else:
            await revoke_role(engine, user_id, body.role)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail=f"角色 {body.role!r} 不存在（先播种 RBAC）") from exc
    return {"id": user_id, "role": body.role, "action": body.action}
