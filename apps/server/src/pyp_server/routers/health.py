"""健康检查（不依赖 DB，供存活探针 / 冒烟）。"""

from __future__ import annotations

from fastapi import APIRouter
from payipa_contracts import CONTRACT_VERSION
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class Health(BaseModel):
    status: str = "ok"
    contract_version: int = CONTRACT_VERSION


@router.get("/healthz", response_model=Health, summary="健康检查（不连库）")
async def healthz() -> Health:
    return Health()
