"""对外 Dataset API（M4 slice-2）：把已发布的组装产物作只读分页数据集对外开放。

API Key 鉴权（X-API-Key，存 hash 校验）+ scope.datasets 白名单授权；响应 JSON 行 + keyset next_cursor。
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from payipa.db.engine import get_engine
from payipa.deliver.dataset import api_key_allows_dataset, read_dataset, verify_api_key
from sqlalchemy.exc import ProgrammingError

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.get("/{product_code}", summary="对外只读数据集（API Key + scope；JSON 行 + keyset 分页）")
async def get_dataset(
    product_code: str,
    cursor: int = Query(0, ge=0, description="keyset 游标：上一页最后一行 id"),
    limit: int = Query(100, gt=0, le=1000),
    x_api_key: str = Header(..., description="对外 API Key（明文；服务端存 hash 校验）"),
) -> dict:
    scope = await verify_api_key(get_engine("pyp"), x_api_key)
    if scope is None:
        raise HTTPException(status_code=401, detail="invalid or revoked api key")
    if not api_key_allows_dataset(scope, product_code):
        raise HTTPException(status_code=403, detail=f"api key not scoped for dataset {product_code}")
    try:
        rows, next_after = await read_dataset(get_engine("business"), product_code, after_id=cursor, limit=limit)
    except ProgrammingError:  # 产物表尚不存在（未产出过）
        return {"rows": [], "next_cursor": None}
    return {"rows": rows, "next_cursor": next_after}
