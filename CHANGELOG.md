# Changelog

本项目采用语义化版本。面向 Agent 的协议兼容性同时受 `CONTRACT_VERSION` 约束，数据库兼容性由 Alembic head 约束。

## 0.1.0 - 2026-07-12

### Added

- 单主控、PostgreSQL 三库和独立 Agent 的 Compose 部署基线。
- 一次性 Agent 入网、长期节点凭证、撤销与连接代次管理。
- HTTP/Browser 能力探测、域名边界、逐跳 SSRF 防护、响应和结果预算。
- 结果 fencing、本地结果 spool、Outbox claim token 和迟到结果处理。
- 动态数据表台账、跨库 provisioning 状态与自动 reconciliation。
- `test_data_*` 与 `data_*` 物理隔离，测试批次不归档、不触发生产推送。
- 首次使用向导、备份恢复、升级回滚和发布镜像 CI 门禁。

### Security

- 首个管理员初始化要求独立安装码；生产 Cookie 启用 Secure。
- 内部规则读取使用绑定内容哈希的短期令牌，上传与规则令牌域分离。
- 生产拒绝共享 Agent join token、默认密钥、未接线的 Redis/S3 配置和多 worker 拓扑。
