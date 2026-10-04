"""持久化 cron 调度：扫描、到期认领、推进与停用；不启动后台任务。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import Schedule, Source, Task


async def due_schedules(engine_pyp: AsyncEngine) -> list[tuple[int, int, str, str, list[str]]]:
    """返回到点（next_run_at ≤ now 或未初始化）且 enabled 的调度。

    每项 = (schedule_id, task_id, cron_expr, source_uuid, seed_urls)。seed_urls 取自 task.params；触发时据此
    建新批次（复用建源存档的种子）。next_run_at 的推进由调用方在成功建批后调用 :func:`advance_schedule` 完成。
    """
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Schedule.id, Task.id, Schedule.cron_expr, Source.uuid, Task.params)
                .select_from(Schedule.__table__)
                .join(Task.__table__, Schedule.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(
                    Schedule.enabled.is_(True),
                    Source.access_confirmed_at.is_not(None),
                    Source.paused_at.is_(None),
                    (Source.cooldown_until.is_(None)) | (Source.cooldown_until <= func.now()),
                    (Schedule.next_run_at.is_(None)) | (Schedule.next_run_at <= func.now()),
                )
            )
        ).all()
    out: list[tuple[int, int, str, str, list[str]]] = []
    for sched_id, task_id, cron_expr, source_uuid, params in rows:
        seeds = list((params or {}).get("seed_urls") or [])
        out.append((sched_id, task_id, cron_expr, source_uuid, seeds))
    return out


async def advance_schedule(engine_pyp: AsyncEngine, schedule_id: int, next_run_at: datetime) -> None:
    """把调度的下次运行时间推进到 next_run_at（由调用方用 cron 表达式算出）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Schedule.__table__).where(Schedule.id == schedule_id).values(next_run_at=next_run_at))


async def claim_schedule(engine_pyp: AsyncEngine, schedule_id: int, next_run_at: datetime) -> bool:
    """原子认领一次到期触发：仅当调度仍启用且仍到期时推进 next_run_at，返回是否认领成功。

    认领成功者才建批次（DB-010）：多进程/重复 tick 对同一到期时间点只会有一个赢家；
    建批次前崩溃最多漏触发一次（cron 语义可接受），不会重复触发。
    """
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Schedule.__table__)
            .where(
                Schedule.id == schedule_id,
                Schedule.enabled.is_(True),
                (Schedule.next_run_at.is_(None)) | (Schedule.next_run_at <= func.now()),
            )
            .values(next_run_at=next_run_at)
        )
    return bool(res.rowcount)


async def disable_schedule(engine_pyp: AsyncEngine, schedule_id: int) -> None:
    """停用调度（如 cron 表达式非法）；避免坏调度每 tick 反复到期。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(update(Schedule.__table__).where(Schedule.id == schedule_id).values(enabled=False))
