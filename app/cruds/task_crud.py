import uuid

from sqlmodel import Session, select

from app.models.task_model import Task, TaskCreate
from app.utils import SecureUtil


def create_task(*, session: Session, task_in: TaskCreate, user_id: uuid.UUID) -> Task:
    fp = SecureUtil.md5([task_in.source_platform, task_in.task_content])

    db_obj = Task.model_validate(
        task_in,
        update={
            "user_id": user_id,
            "task_fingerprint": fp,
            "status": 1,
        },
    )

    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def get_task_by_fp(session, task) -> Task:
    task_md5 = SecureUtil.md5([task.source_platform, task.task_content])
    statement = select(Task).where(Task.task_fingerprint == task_md5)
    session_task = session.exec(statement).first()
    return session_task


def check_repeat_task_name(session, task_name, user_id) -> Task:
    statement = select(Task).where(Task.task_name == task_name, Task.user_id == user_id)
    session_task = session.exec(statement).first()
    return session_task
