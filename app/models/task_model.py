import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import SMALLINT, TEXT, DateTime
from sqlalchemy.dialects.postgresql import JSON
from sqlmodel import Field, SQLModel

from app.models.basic_model import get_datetime_utc


class TaskStatus(Enum):
    PENDING = 0
    BEGIN = 1
    IN_PROGRESS = 2
    COMPLETED = 3
    FAILED = -1
    KILL = -2


class TaskBase(SQLModel):
    task_group: str = Field(
        default="default group", max_length=100, index=True, sa_column_kwargs={"comment": "任务分组"}
    )
    task_name: str = Field(default="default name", max_length=200, index=True, sa_column_kwargs={"comment": "任务名称"})
    task_content: dict[str, Any] = Field(default={}, sa_type=JSON, sa_column_kwargs={"comment": "任务表单内容"})
    source_platform: str = Field(default="", max_length=50, index=True, sa_column_kwargs={"comment": "资源平台"})


class TaskCreate(TaskBase):
    pass


class TaskUpdate(SQLModel):
    task_group: str | None = ""
    task_name: str | None = ""
    is_delete: bool | None = False


class Task(TaskBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_fingerprint: str = Field(max_length=32, unique=True, index=True)

    status: int = Field(default=0, sa_type=SMALLINT, sa_column_kwargs={"comment": "任务状态"})
    is_delete: bool = False

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    # 关联用户
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)


class TaskPublic(TaskBase):
    id: uuid.UUID
    task_fingerprint: str
    status: int
    is_delete: bool
    created_at: datetime
    updated_at: datetime


class TasksPublic(SQLModel):
    data: list[TaskPublic]
    count: int


class TaskRun(SQLModel, table=True):
    __tablename__ = "task_run"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_fingerprint: str = Field(max_length=32, unique=True, index=True)
    source_platform: str = Field(default="", max_length=50, index=True, sa_column_kwargs={"comment": "资源平台"})
    priority: int = Field(default=5, sa_type=SMALLINT, sa_column_kwargs={"comment": "任务优先级 数字越大越高"})
    cron_expr: str = Field(default="", max_length=50, sa_column_kwargs={"comment": "CRON定时器"})
    crawl_task: dict[str, Any] = Field(default={}, sa_type=JSON, sa_column_kwargs={"comment": "抓取任务"})
    status: int = Field(default=0, sa_type=SMALLINT, sa_column_kwargs={"comment": "抓取任务状态"})
    error_reason: str | None = Field(sa_type=TEXT, sa_column_kwargs={"comment": "抓取任务异常原因"})
    enabled: bool = False
    started_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    finished_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
