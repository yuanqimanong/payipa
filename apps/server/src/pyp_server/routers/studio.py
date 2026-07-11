"""组装（studio）写操作 API：版本状态机流转 + 发布签名门。

发布（→active）时写 HMAC 签名（红线7：AI 产物必经验证 + 签名，执行器运行前校验）。签名密钥复用
`upload_secret` 域（与 job_token 同，不入库）。draft↔testing 用 status 端点，发布用 publish 端点。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.studio.store import AssemblyStore
from pydantic import BaseModel, Field

from pyp_server.auth import require_perm

router = APIRouter(prefix="/api/assemblies", tags=["studio"])


def _store() -> AssemblyStore:
    return AssemblyStore(get_engine("pyp"))


async def _require_exists(assembly_id: int) -> None:
    if await _store().get(assembly_id) is None:
        raise HTTPException(status_code=404, detail=f"组装 id={assembly_id} 不存在")


class AsmStatusRequest(BaseModel):
    status: Literal["draft", "testing"] = Field(..., description="draft=草稿 / testing=测试；发布(active)请用 publish")


@router.post(
    "/{assembly_id}/publish",
    summary="发布组装（status=active + 写 HMAC 签名门）",
    dependencies=[Depends(require_perm("assemblies.publish"))],
)
async def publish_assembly(assembly_id: int) -> dict:
    await _require_exists(assembly_id)
    sig = await _store().publish(assembly_id, get_db_settings().upload_secret)
    return {"id": assembly_id, "status": "active", "signed": bool(sig)}


@router.post(
    "/{assembly_id}/status",
    summary="组装状态流转（draft / testing；发布用 publish 端点）",
    dependencies=[Depends(require_perm("assemblies.write"))],
)
async def set_assembly_status(assembly_id: int, body: AsmStatusRequest) -> dict:
    await _require_exists(assembly_id)
    await _store().set_status(assembly_id, body.status)
    return {"id": assembly_id, "status": body.status}
