import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

from app.models.basic_model import get_datetime_utc


class DataQueryConfig(SQLModel, table=True):
    __tablename__ = "data_query_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(default="", max_length=50, unique=True, index=True, sa_column_kwargs={"comment": "名称"})
    db_name: str = Field(default="", max_length=50, sa_column_kwargs={"comment": "数据库"})
    table_name: str = Field(default="", max_length=50, sa_column_kwargs={"comment": "数据表"})
    db_uri: str | None = Field(default="", max_length=500, sa_column_kwargs={"comment": "数据连接"})

    enabled: bool = False
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class DataQuery(SQLModel, table=True):
    __tablename__ = "data_query"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(default="", max_length=50, unique=True, index=True, sa_column_kwargs={"comment": "名称"})
    sql: str = Field(default="", max_length=1000, sa_column_kwargs={"comment": "SQL 查询"})
    config_id: uuid.UUID

    enabled: bool = False
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class DataQueryConfigPublic(SQLModel):
    id: uuid.UUID
    name: str
    created_at: datetime | None = None


class DatasQueryConfigPublic(SQLModel):
    data: list[DataQueryConfigPublic]
    count: int


class DataQueryPublic(SQLModel):
    id: uuid.UUID
    name: str
    sql: str
    created_at: datetime | None = None


class DatasQueryPublic(SQLModel):
    data: list[DataQueryPublic]
    count: int


class QueryDataPublic(SQLModel):
    id: int
    url: str
    title: str
    publish_time: datetime


class QueryDatasPublic(SQLModel):
    data: list[QueryDataPublic]
    count: int


class QueryDataPublicDetail(SQLModel):
    id: int
    url: str
    cover_url: str
    title: str
    description: str
    author: str
    publish_time: datetime
    content: str
