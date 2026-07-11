# CLAUDE.md — payipa 开发约定

> 权威设计文档在 `docs/`（实现方案；`软件需求与详细设计说明书.md` 即 SDD + `决策记录.md`）。**冲突时以 SDD + 决策记录为准。** 本文件是给 Claude/开发者的落地约定速查。

## 项目用途与数据访问边界

- payipa 用于处理**用户有权访问**的数据源：自有系统、已授权 API、明确允许自动访问的公开页面，以及经数据提供方书面许可的其他来源。
- Connector 必须遵守数据源的服务条款、API 文档、`robots.txt`、认证边界和速率限制；授权依据、账号归属与数据保留要求应随数据源配置留档。
- 认证失败、授权拒绝、需人工确认的访问挑战或明确拒绝自动访问的响应，一律暂停对应数据源并提示人工复核；不得通过更换账号、会话或出口自动继续请求。
- 网络出口由部署环境统一配置，应用、任务和规则不得动态选择或切换出口。浏览器自动化只使用标准 Playwright 完成正常渲染和已授权登录流程。

## 不可违背的铁律（来自已定决策）

- **Python 3.14 固定**（payipa `>=3.14`，生产普通 GIL 构建，不开 free-threaded）；**uv** 管理；**ruff** + **import-linter**（CI 强制模块边界）。
- **双仓 + path editable**：`payipa/` 与 `jianbing_utils/` 是 `project/` 下**兄弟目录**。jb `requires-python>=3.11`（多项目复用），payipa `>=3.14`。
- **依赖方向**：`server → core → contracts`，`agent → contracts`；**pyp-agent 禁止依赖 payipa-core**。contracts 零 I/O、零逻辑、仅依赖 pydantic。
- **契约先行**：`payipa-contracts` 的 Pydantic schema + 负数错误码枚举是第一产出；字段带 description 并标注「已生效/未生效」。
- **PG 三库**：`pyp`(平台) / `data_center`(采集数据 `data_*`) / `business`(组装产物 `asm_*`)；库名可配、从 `.env` 读；迁移用 Alembic。跨 database 不 join。
- **架构红线（SDD 00 §3.8，10 条，CI/评审对照）**：API 不直接跑爬虫；用户/AI 代码不直连 DB（沙箱 + Query Gateway）；外部采集不超出授权与站点规则；大对象走对象存储不进控制面（Redis/WS/HTTP JSON）；raw 留存；Connector/规则/组装/推送版本化；AI 产物必经 test 验证 + 签名；契约字段诚实标注；凭证分层、脚本不接触明文、token 存 hash；默认开 TLS 校验。

## 双仓纪律（务必遵守）

- **本地开发**：`payipa/pyproject.toml` 的 `[tool.uv.sources]` 用 **editable path** 引 `../jianbing_utils`（jb 改码即时生效、无版本问题）。
- **提交 payipa 前**：先给 jb 打并推 tag（`uv version --bump` → `git tag vX.Y.Z` → push），再把 path 源替换为 **git 源**：
  ```toml
  jianbing-utils = { git = "https://github.com/yuanqimanong/jianbing_utils.git", tag = "v0.1.0" }
  ```
  然后 `uv lock` 提交。**path 源绝不进仓库**（会让 lock 锁成本地路径、CI 失败、机器人升级失效）。
- **jb 发版**：`uv build --no-sources` 验证可脱离本机构建（依赖须全部来自公开 index）；tag `v*` 触发 Trusted Publishing 发 PyPI。
- **payipa 跟进 jb 新版**：`uv lock --upgrade-package jianbing-utils`。

## 工程约定

- **src layout**，构建后端统一 `uv_build`（payipa-core 因导入名 `payipa` ≠ 项目名，用 `[tool.uv.build-backend] module-name = "payipa"`）。
- **并发底座 anyio**（asyncio 事件循环 + trio 式结构化并发；task group / cancel scope 整树取消）；主控与 agent 两侧统一。
- **ORM**：SQLAlchemy 2.0 async + asyncpg；配置用 pydantic-settings 读 `.env`。**SQLAlchemy 模型属 core 的持久化细节，绝不放 contracts**（contracts 只描述传输形状，DB schema 可与之不同）。
- **数据表**：`data_*` / `asm_*` 是**运行时动态表（加法演进：JSONB + 勾索引 STORED 生成列）**，**不进 Alembic**——由 core 建源/组装时程序化 DDL。Alembic 只管 `pyp` 平台表 + `data_center.artifacts` 固定表。
- **correlation id**：`task_id / batch_id / attempt_id` 贯穿 structlog 日志、artifacts、task_events。
- **requests.state**（smallint）：正数正常态（0 排队/1 已分派/2 运行/3 成功/4 取消），负数错误码（见 `payipa_contracts.errors`）。

## 里程碑（SDD §12；截至 2026-07-11 · 权威状态见 docs/README.md 实现进度表）

- **M0–M6 均已完成、CI 绿**（护城河策略「先建接口/边界，再逐阶段填能力」已执行到 M6，非早期阶段）：
  - **M0** 双仓 + workspace + contracts 全 schema + errors 枚举 + import-linter CI + Alembic 三库迁移。
  - **M1** 单源端到端（建源→agent WS 抓取→raw 存档→data_center→Tabulator 查看页）+ 声明式解释器。
  - **M2** 持久化派发环 + 租约回收 + 断连重排 + 多波爬行 + 优先级 + cron + 取消 + 节点注册/加权分组亲和 + 每源限流/AIMD。
  - **M3** Query Gateway（红线2 唯一取数）+ job_token + 签名游标/配额 + 版本签名门 + 增量水位 + SchemaEvolver + **真 SandboxExecutor（Docker/WSL2 锁定容器 + 路径白名单 egress）+ 常驻 worker 池 SandboxPool**。
  - **M4** outbox + 对外 Dataset API + 隔离子进程投递（目标域白名单 + KEK 凭证）+ 通知机器人 + 自动/手动/cron 三触发。
  - **M5** RBAC + monitor 聚合 + AI/LLM Gateway（自研 provider 抽象 + 官方 anthropic SDK + echo 离线）+ 标准 Playwright 浏览器引擎 + 点选插件；**管理台 12 页真数据 + 全量写操作 UI**。
  - **M6** 采集韧性（响应分类/Retry-After/请求退避/源冷却/整源暂停）+ 生产安全加固（preflight/CSRF/join token/强制登录/僵尸节点判定）。
- 剩余仅**需外部运行时的活验/独立服务**：真 provider key、真浏览器端到端冒烟、worker 池独立机器部署、Langfuse 自部署（均在代码/文档诚实标注）。

## 常用命令

```bash
uv sync --all-packages                         # 装齐工作区
uv run uvicorn pyp_server.main:app --reload     # 起主控
uv run ruff check && uv run ruff format --check # 代码质量
uv run lint-imports                             # 模块边界
uv run pytest                                   # 测试
uv run alembic -c deploy/alembic.ini upgrade heads   # 三库迁移（需活 PG）
```

## 待确认/实现期产出（SDD §14，不阻塞开工，落地时定）

错误码枚举细化 · 固定方法 SDK 清单 · 分表命名规则 · 各类默认值（槽位 N/租约/调频阈值/worker 池/raw GC）· 选择器库二选一 · Alembic 版本核验 · Langfuse 自部署形态。

## 产品化待办（记录，整体跑通后再完善）

- **首次运行安装向导**：系统安装后首次启动，引导管理员在页面上配置数据库连接（三库）等基础设施信息，
  并初始化管理员账号 / 建库建表（跑迁移）。M0–M1 阶段先走 `.env` + `alembic upgrade`，不做此向导；
  等平台整体跑起来后补一个 setup wizard（检测未初始化 → 引导页 → 写配置 → 迁移 → 建首个管理员）。
