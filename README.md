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
# 1) 安装锁定依赖并生成部署配置
uv sync --all-packages --locked
uv run pypctl init

# 2) 构建并启动三库与主控
docker compose -f deploy/compose.yml --env-file deploy/.env.compose up -d --build db server
uv run pypctl smoke

# 3) 本地质量闸
uv run ruff check && uv run ruff format --check
uv run lint-imports          # 模块边界
uv run pytest
```

完整安装顺序、首个管理员和 Agent 接入见 [QUICKSTART.md](QUICKSTART.md)。健康端点为 `/livez`、`/readyz` 和 `/version`。

当前 `0.1.x` 支持 Linux 单主控、单 worker、PostgreSQL 三库和本地持久卷。S3、Redis 和多主控尚未接线，配置后会在启动阶段明确拒绝。许可证见 [LICENSE](LICENSE)，漏洞请按 [SECURITY.md](SECURITY.md) 私密报告。

> 开发约定见 [CLAUDE.md](CLAUDE.md)。权威设计见 [docs/](docs/README.md)（实现方案；SDD + 决策记录为准）。
