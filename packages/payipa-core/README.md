# payipa-core

payipa 全部业务逻辑（导入名 `payipa`）。模块：`crawl`（采集/调度/限流/连接器）、`explore`（查询/SQL 网关/导出）、`studio`（组装/Query Gateway/装载）、`deliver`（推送/Outbox/Dataset API）、`ai`（PydanticAI 接入）、`proxy`（代理池/中转/溯源）、`monitor`（聚合统计）、`storage`（s3/local 后端）。

`db/` 为持久化基座：三库（`pyp`/`data_center`/`business`）各一 MetaData、async engine（asyncpg）、SQLAlchemy 2.0 模型。依赖方向：`core → contracts`。
