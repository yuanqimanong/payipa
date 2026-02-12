import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import MetaData, Table, inspect, text
from sqlmodel import Session, col, func, select

from app.api.deps import CurrentUser, DCSessionDep, SessionDep, get_current_active_superuser
from app.cruds import task_crud
from app.models.basic_model import Message
from app.models.data_model import DataQueryConfig, DatasQueryConfigPublic, QueryDatasPublic

router = APIRouter(prefix="/datas", tags=["datas"])


@router.get("/configs", response_model=DatasQueryConfigPublic)
def read_configs(session: SessionDep, skip: int = 0, limit: int = 100) -> Any:
    count_statement = select(func.count()).select_from(DataQueryConfig).where(DataQueryConfig.enabled)
    count = session.exec(count_statement).one()

    statement = (
        select(DataQueryConfig)
        .where(DataQueryConfig.enabled)
        .order_by(col(DataQueryConfig.created_at).desc())
        .offset(skip)
        .limit(limit)
    )
    data_query = session.exec(statement).all()

    return DatasQueryConfigPublic(data=data_query, count=count)


@router.get("/{table_name}", response_model=QueryDatasPublic)
def query_datas(
    session: DCSessionDep, table_name: str, current_user: CurrentUser, skip: int = 0, limit: int = 100
) -> Any:
    # 1. 安全检查：验证表是否存在
    # 使用 inspect 检查数据库中实际存在的表名，防止 SQL 注入和 404
    engine = session.connection().engine
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    # 2. 动态加载表结构 (Reflection)
    metadata = MetaData()
    try:
        # autoload_with 会自动从数据库读取字段信息
        table = Table(table_name, metadata, autoload_with=engine)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error loading table: {str(e)}")

    # 3. 构建查询 (更优雅的 Pythonic 方式)

    # 3.1 计算总数
    # 相当于: SELECT count(*) FROM table_name
    count_query = select(func.count()).select_from(table)
    count = session.exec(count_query).one()

    # 3.2 查询数据
    # 你指定了 id, url, title, publish_time，我们需要检查表里是否有这些字段
    # 如果你不确定字段是否存在，可以用 hasattr(table.c, 'field_name') 判断，或者直接查所有 table

    # 方式 A: 查特定字段 (如果字段不存在会报错，所以最好做个简单的检查或者 try-catch)
    target_cols = ["id", "url", "title", "publish_time"]
    selected_columns = []

    for col_name in target_cols:
        if col_name in table.c:
            selected_columns.append(table.c[col_name])

    if not selected_columns:
        # 如果都没有，就查所有字段
        query = select(table)
    else:
        query = select(*selected_columns)

    # 添加排序、分页
    # 注意：如果 publish_time 不存在，这里会报错，需要防御性编程
    if "publish_time" in table.c:
        query = query.order_by(table.c.publish_time.desc())

    query = query.offset(skip).limit(limit)

    # 4. 执行查询
    result = session.exec(query)

    # 5. 格式化结果
    # session.exec 返回的是 Row 对象，类似于 NamedTuple
    # 如果查询的是部分字段，mappings().all() 能更好转为字典
    rows = result.mappings().all()

    return QueryDatasPublic(data=rows, count=count)
