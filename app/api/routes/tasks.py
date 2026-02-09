from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import col, func, select

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.cruds import task_crud, user_crud
from app.models.task_model import Task, TaskCreate, TaskPublic, TasksPublic

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get(
    "/",
    dependencies=[Depends(get_current_active_superuser)],
    response_model=TasksPublic,
)
def read_tasks(session: SessionDep, skip: int = 0, limit: int = 100) -> Any:
    count_statement = select(func.count()).select_from(Task)
    count = session.exec(count_statement).one()

    statement = select(Task).order_by(col(Task.created_at).desc()).offset(skip).limit(limit)
    tasks = session.exec(statement).all()

    return TasksPublic(data=tasks, count=count)


@router.post("/", response_model=TaskPublic)
def create_task(*, session: SessionDep, task_in: TaskCreate, current_user: CurrentUser) -> Any:
    existing_task = task_crud.get_task_by_fp(session=session, task=task_in)
    if existing_task:
        raise HTTPException(
            status_code=400,
            detail=f"该任务已在系统中存在：[{existing_task.task_group} - {existing_task.task_name}]",
        )

    task = task_crud.create_task(session=session, task_in=task_in, user_id=current_user.id)
    return task
