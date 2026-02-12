import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, col, func, select

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.cruds import task_crud
from app.models.basic_model import Message
from app.models.task_model import Task, TaskCreate, TaskPublic, TasksPublic, TaskStatus, TaskUpdate
from app.models.user_model import User

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
def create_task(*, session: SessionDep, user_in: TaskCreate, current_user: CurrentUser) -> Any:
    existing_task = task_crud.get_task_by_fp(session=session, task=user_in)
    if existing_task:
        raise HTTPException(
            status_code=400,
            detail=f"该任务已在系统中存在：[{existing_task.task_group} - {existing_task.task_name}]",
        )
    repeat_name = task_crud.check_repeat_task_name(
        session=session, task_name=user_in.task_name, user_id=current_user.id
    )
    if repeat_name:
        raise HTTPException(
            status_code=400,
            detail=f"该任务名称 [{user_in.task_name}] 重复，请更换名称。",
        )

    task = task_crud.create_task(session=session, task_in=user_in, user_id=current_user.id)
    return task


@router.patch("/{task_id}", response_model=TaskPublic)
def update_task(*, session: SessionDep, task_id: uuid.UUID, user_in: TaskUpdate, current_user: CurrentUser) -> Any:
    """
    Update a task.
    """

    db_task = task_checker(session, current_user, task_id)

    db_task = task_crud.update_task(session=session, db_task=db_task, user_in=user_in)
    return db_task


@router.patch("/{task_id}/begin/", response_model=TaskPublic)
def begin_task(*, session: SessionDep, task_id: uuid.UUID, current_user: CurrentUser) -> Any:
    """
    Begin a task.
    """

    db_task = task_checker(session, current_user, task_id)
    try:
        task_crud.create_task_run(session=session, db_task=db_task)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    db_task = task_crud.update_status(session=session, db_task=db_task, user_in=TaskStatus.BEGIN)
    return db_task


@router.patch("/{task_id}/stop/", response_model=TaskPublic)
def stop_task(*, session: SessionDep, task_id: uuid.UUID, current_user: CurrentUser) -> Any:
    """
    Stop a task.
    """

    db_task = task_checker(session, current_user, task_id)
    task_crud.update_task_run(session=session, db_task=db_task, user_in={"status": TaskStatus.KILL.value})
    db_task = task_crud.update_status(session=session, db_task=db_task, user_in=TaskStatus.KILL)
    return db_task


@router.patch("/{task_id}/remove/")
def remove_task(*, session: SessionDep, task_id: uuid.UUID, current_user: CurrentUser) -> Message:
    """
    Remove a task.
    """

    db_task = task_checker(session, current_user, task_id)
    db_task.is_delete = True

    task_crud.update_task_run(session=session, db_task=db_task, user_in={"status": TaskStatus.KILL.value})

    session.add(db_task)
    session.commit()
    session.refresh(db_task)
    return Message(message="任务已成功删除")


def task_checker(session: Session, current_user: User, task_id: UUID) -> type[Task] | None:
    db_task = session.get(Task, task_id)
    if not db_task:
        raise HTTPException(
            status_code=404,
            detail="该任务在系统中不存在",
        )
    if db_task.user_id != current_user.id and current_user.is_superuser is False:
        raise HTTPException(status_code=403, detail="用户没有足够的权限")
    return db_task


@router.get("/me", response_model=TasksPublic)
def read_tasks_me(session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100) -> Any:
    count_statement = select(func.count()).select_from(Task).where(Task.user_id == current_user.id)
    count = session.exec(count_statement).one()

    statement = (
        select(Task)
        .where(Task.user_id == current_user.id)
        .order_by(col(Task.created_at).desc())
        .offset(skip)
        .limit(limit)
    )
    tasks = session.exec(statement).all()

    return TasksPublic(data=tasks, count=count)


@router.get("/group", response_model=TasksPublic)
def read_tasks_by_group(
    session: SessionDep, current_user: CurrentUser, task_group: str, skip: int = 0, limit: int = 100
) -> Any:
    count_statement = select(func.count()).select_from(Task).where(Task.task_group == task_group)
    count = session.exec(count_statement).one()

    statement = (
        select(Task)
        .where(Task.user_id == current_user.id)
        .where(Task.task_group == task_group)
        .order_by(col(Task.created_at).desc())
        .offset(skip)
        .limit(limit)
    )
    tasks = session.exec(statement).all()

    return TasksPublic(data=tasks, count=count)
