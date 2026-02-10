import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSON
from sqlmodel import Field, SQLModel

from app.models.basic_model import get_datetime_utc


class Rule(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_platform: str = Field(
        default="", max_length=50, unique=True, index=True, sa_column_kwargs={"comment": "资源平台"}
    )
    form_rule: dict[str, Any] = Field(default={}, sa_type=JSON, sa_column_kwargs={"comment": "表单生成规则"})
    crawl_rule: dict[str, Any] = Field(default={}, sa_type=JSON, sa_column_kwargs={"comment": "抓取逻辑规则"})

    enabled: bool = False
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
