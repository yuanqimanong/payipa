import uuid
from typing import Any

from sqlmodel import Session, select

from app.models.spider_model import Rule
from app.models.task_model import Task, TaskCreate, TaskRun
from app.utils import SecureUtil


def create_task(*, session: Session, task_in: TaskCreate, user_id: uuid.UUID) -> Task:
    fp = SecureUtil.md5([task_in.source_platform, task_in.task_content])

    db_obj = Task.model_validate(
        task_in,
        update={
            "user_id": user_id,
            "task_fingerprint": fp,
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


def update_task(session, db_task, user_in) -> Any:
    task_data = user_in.model_dump(exclude_unset=True)
    db_task.sqlmodel_update(task_data)
    session.add(db_task)
    session.commit()
    session.refresh(db_task)
    return db_task


def update_status(session, db_task, user_in) -> Any:
    db_task.sqlmodel_update({"status": user_in.value})
    session.add(db_task)
    session.commit()
    session.refresh(db_task)
    return db_task


def create_task_run(session, db_task: Task):
    task_content = db_task.task_content
    source_platform = db_task.source_platform

    rule_statement = select(Rule).where(Rule.source_platform == source_platform)
    db_rule = session.exec(rule_statement).first()

    if not db_rule or not task_content:
        raise Exception("抓取规则设置错误")

    crawl_rule = db_rule.crawl_rule["basic"]
    crawl_task = {k: v for k, v in task_content.items() if k in crawl_rule}

    new_task_run = TaskRun(
        task_fingerprint=db_task.task_fingerprint,
        source_platform=source_platform,
        priority=task_content["priority"],
        cron_expr=task_content["cron_expr"],
        crawl_task=crawl_task,
    )

    task_run_statement = select(TaskRun).where(TaskRun.task_fingerprint == db_task.task_fingerprint)
    db_task_run = session.exec(task_run_statement).first()
    if db_task_run:
        db_task_run.sqlmodel_update(new_task_run)
    else:
        db_task_run = new_task_run
    session.add(db_task_run)
    session.commit()
    session.refresh(db_task_run)


def update_task_run(session, db_task, user_in):
    task_run_statement = select(TaskRun).where(TaskRun.task_fingerprint == db_task.task_fingerprint)
    db_task_run = session.exec(task_run_statement).first()
    db_task_run.sqlmodel_update(user_in)
    session.add(db_task_run)
    session.commit()
    session.refresh(db_task_run)
