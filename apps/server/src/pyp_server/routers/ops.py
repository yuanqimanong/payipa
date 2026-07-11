"""运行中心（P1-30 轻量版）：正在运行 + 最近完成的批次一览，顶栏下拉直达。

完整版（持久 operation 表、导出/发布等长任务统一追踪）随 P1-30 实施；本版先让
「重跑/采集跑到哪了」离开原页面也能找回——数据即 batches（真实运行态，无新表）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from payipa.db.engine import get_engine
from payipa.db.pyp import Batch, Source, Task
from sqlalchemy import select

from pyp_server.auth import require_user

router = APIRouter(prefix="/api/ops", tags=["ops"], dependencies=[Depends(require_user)])


@router.get("/recent", summary="运行中心：在跑 + 最近批次")
async def recent(limit: int = Query(8, ge=1, le=50)) -> list[dict]:
    """running 批次置顶 + 最近批次补足 limit；带源名，前端可再拉 /api/monitor/batches/{id} 看进度。"""
    async with get_engine("pyp").connect() as conn:
        rows = (
            await conn.execute(
                select(
                    Batch.id,
                    Batch.status,
                    Batch.created_at,
                    Batch.finished_at,
                    Source.name,
                    Source.uuid,
                )
                .select_from(Batch.__table__)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .order_by((Batch.status == "running").desc(), Batch.id.desc())
                .limit(limit)
            )
        ).all()
    return [
        {
            "batch_id": r[0],
            "status": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "finished_at": r[3].isoformat() if r[3] else None,
            "source_name": r[4],
            "source": r[5],
        }
        for r in rows
    ]
