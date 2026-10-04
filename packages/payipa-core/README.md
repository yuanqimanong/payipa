# payipa-core

payipa 业务逻辑（导入名 `payipa`）。模块：`crawl`（采集生命周期）、`explore`（结构化查询/导出）、`studio`（组装/Query Gateway/装载）、`deliver`（推送/Outbox/Dataset API）、`ai`（provider/LLM Gateway）、`monitor`（聚合统计）、`storage`（本地后端；S3 接线尚未实现）。

`db/` 为持久化基座：三库（`pyp`/`data_center`/`business`）各一 MetaData、async engine（asyncpg）、SQLAlchemy 2.0 模型。依赖方向：`core → contracts`。

`crawl.service.CrawlService` 接收平台库与数据面引擎，统一建源、规则保存、动态表 provisioning 和批次创建。
`sources` / `batches` / `nodes` / `dispatch` / `results` / `frontier` / `resilience` / `schedules` 各自管理一种职责，
`_policy` 只放纯策略；`run` 是旧导入路径的兼容 facade。新代码直接导入职责模块，import-linter 禁止重新依赖 facade。

完整约束与验证见 [架构重构与边界](../../docs/15-架构重构与边界.md)。
