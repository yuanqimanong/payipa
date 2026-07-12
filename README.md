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
# 在 payipa 仓库根执行：安装锁定依赖并生成本地试用配置
uv sync --package pyp-server --locked
uv run pypctl init

# 启动前检查，再构建并启动三库、迁移和主控
uv run pypctl doctor
uv run pypctl up --build
uv run pypctl smoke
```

随后打开 `http://127.0.0.1:8100/setup`，使用 `deploy/.env.compose` 中的安装码创建首个管理员。生产环境请改用 `uv run pypctl init --production-host pyp.example.com`，完整安装顺序、TLS、首个管理员、Agent 接入与验收见 [Docker Compose 安装与验收](docs/install/docker-compose.md)。健康端点为 `/livez`、`/readyz` 和 `/version`。

当前 `0.1.x` 支持 Linux 单主控、单 worker、PostgreSQL 三库和本地持久卷。S3、Redis 和多主控尚未接线，配置后会在启动阶段明确拒绝。许可证见 [LICENSE](LICENSE)，漏洞请按 [SECURITY.md](SECURITY.md) 私密报告。

> 开发约定见 [CLAUDE.md](CLAUDE.md)。权威设计见 [docs/](docs/README.md)（实现方案；SDD + 决策记录为准）。
