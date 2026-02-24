import json
from json import JSONDecodeError
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from sqlalchemy import MetaData, Table, inspect, text
from sqlmodel import col, func, select

from app.api.deps import CurrentUser, DCSessionDep, SessionDep
from app.models.basic_model import Message
from app.models.data_model import (
    DataQuery,
    DataQueryConfig,
    DatasQueryConfigPublic,
    DatasQueryPublic,
    QueryDataPublicDetail,
    QueryDatasPublic,
)

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


@router.get("/table/{config_id}", response_model=QueryDatasPublic)
def query_datas(
    session: SessionDep,
    dc_session: DCSessionDep,
    config_id: str,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    statement = select(DataQueryConfig).where(DataQueryConfig.id == config_id)
    data_query = session.exec(statement).one()
    table_name = data_query.table_name

    # 1. 安全检查：验证表是否存在
    # 使用 inspect 检查数据库中实际存在的表名，防止 SQL 注入和 404
    engine = dc_session.connection().engine
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
    count = dc_session.exec(count_query).one()

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
    result = dc_session.exec(query)

    # 5. 格式化结果
    # session.exec 返回的是 Row 对象，类似于 NamedTuple
    # 如果查询的是部分字段，mappings().all() 能更好转为字典
    rows = result.mappings().all()

    return QueryDatasPublic(data=rows, count=count)


@router.get("/sqls/{config_id}", response_model=DatasQueryPublic)
def query_datas_sqls(
    session: SessionDep, current_user: CurrentUser, config_id: str, skip: int = 0, limit: int = 100
) -> Any:
    count_statement = select(func.count()).select_from(DataQuery).where(DataQuery.config_id == config_id)
    count = session.exec(count_statement).one()

    statement = (
        select(DataQuery)
        .where(DataQuery.config_id == config_id)
        .order_by(col(DataQuery.created_at).desc())
        .offset(skip)
        .limit(limit)
    )
    data_query = session.exec(statement).all()

    return DatasQueryPublic(data=data_query, count=count)


@router.get("/sql/{sql_id}", response_model=QueryDatasPublic)
def query_datas_by_sql_id(
    session: SessionDep,
    dc_session: DCSessionDep,
    sql_id: str,
    current_user: CurrentUser,
) -> Any:
    statement = select(DataQuery).where(DataQuery.id == sql_id)
    data_query = session.exec(statement).one()

    sql_statement = text(data_query.sql)
    result = dc_session.execute(sql_statement)
    results = result.mappings().all()
    return QueryDatasPublic(data=results, count=len(results))


@router.get("/{config_id}/detail/{detail_id}", response_model=QueryDataPublicDetail)
def query_data_detail(
    session: SessionDep,
    dc_session: DCSessionDep,
    config_id: str,
    detail_id: int,
    current_user: CurrentUser,
) -> Any:
    statement = select(DataQueryConfig).where(DataQueryConfig.id == config_id)
    data_query = session.exec(statement).one()
    table_name = data_query.table_name

    sql_statement = text(f"SELECT * FROM {table_name} WHERE id = {detail_id};")
    result_statement = dc_session.execute(sql_statement)
    result = result_statement.mappings().one()
    result_dict = dict(result)
    try:
        content_json = json.loads(result_dict["content"])
        result_dict["content"] = "\n".join([f"{cj['title']}:\n{cj['body']}" for cj in content_json])
    except JSONDecodeError:
        pass

    return QueryDataPublicDetail(**result_dict)


@router.get("/{config_id}/detail/{detail_id}/send_ghost")
async def query_data_detail_send_ghost(
    session: SessionDep,
    config_id: str,
    detail_id: int,
    current_user: CurrentUser,
) -> Message:
    statement = select(DataQueryConfig).where(DataQueryConfig.id == config_id)
    data_query = session.exec(statement).one()
    table_name = data_query.table_name

    url = "http://127.0.0.1:22333/crawler/ghost_api"
    # url = "http://192.168.14.60:22333/crawler/ghost_api"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={"table_name": table_name, "record_id": detail_id}, timeout=15)

        if response.status_code == 200:
            return Message(message=f"Ghost 发送成功：{table_name} - {detail_id}")
        else:
            return Message(message=f"Ghost 发送失败：{response.text}")
