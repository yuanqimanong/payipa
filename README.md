# payipa / 爬亿爬

源无关的「数据获取 → 清洗 → 查询 → 组装 → 推送」一站式数据接入与加工平台。monorepo（uv workspace）。

## 结构

```
apps/server            主控进程（FastAPI 薄入口，装配 core）        导入名 pyp_server
packages/payipa-contracts  契约包（Pydantic schema，零 I/O）        导入名 payipa_contracts
packages/payipa-core       全部业务逻辑                              导入名 payipa
packages/pyp-agent         子节点 agent（只依赖 contracts）          导入名 pyp_agent
extensions/picker      浏览器点选插件（MV3，非 Python）
deploy/                docker compose / Dockerfile / alembic（三库迁移）
tests/                 跨包集成/冒烟测试
```

依赖方向（import-linter CI 强制）：`server → core → contracts`，`agent → contracts`（**agent 禁止 → core**）。

## 快速开始

```bash
# 1) 本地开发：让 jianbing_utils（兄弟目录）以 editable 引入（jb 改码即时生效）
#    根 pyproject 的 [tool.uv.sources] 已用 path 源，直接 sync 即可
uv sync --all-packages

# 2) 起主控
uv run uvicorn pyp_server.main:app --reload
#    OpenAPI:  http://127.0.0.1:8000/openapi.json   Swagger: /docs   健康: /healthz

# 3) 质量闸
uv run ruff check && uv run ruff format --check
uv run lint-imports          # 模块边界
uv run pytest
```

三库连接从 `../project/.env`（或环境变量）读：`PG_HOST/PG_PORT/PG_USER/PG_PASSWORD` + `PG_DB_PYP`/`PG_DB_DATA_CENTER`/`PG_DB_BUSINESS`。迁移：`uv run alembic -c deploy/alembic.ini upgrade heads`。

> 开发约定、双仓纪律、里程碑见 [CLAUDE.md](CLAUDE.md)。权威设计见 [docs/](docs/README.md)（实现方案；SDD + 决策记录为准）。
