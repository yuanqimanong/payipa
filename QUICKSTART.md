# QUICKSTART —— 本地手动测试（M1 + 登录 + 建源界面）

> 现可用：**安装 → 登录 → 一次性接入 Agent → 建源 → 采集 → 查看数据** 全流程。

## 0. 容器化一键路径（不想装本地 PG/uvicorn 就走这条）

主控 + PostgreSQL 三库 + 迁移 + 可选 agent 已有完整 Compose 编排与 `pypctl` 运维 CLI（P0-02/03）：

```bash
uv sync --package pyp-server --locked
uv run pypctl init
uv run pypctl doctor
uv run pypctl up --build
uv run pypctl smoke
# 然后打开 http://127.0.0.1:8100/setup，用 deploy/.env.compose 中的安装码创建首个管理员
```

这条路径只验证主控与迁移；接入 Agent 后再按安装文档的“首次验收”完成真实采集验证。生产部署请不要先执行默认 `init`，而是按 **[docs/install/docker-compose.md](docs/install/docker-compose.md)** 使用 `pypctl init --production-host` 并配置 TLS。
以下章节是**本地开发路径**（宿主机 PG + uvicorn，便于断点调试）。

## 前置

PostgreSQL 已起（Docker，连接在 `../.env`）；在 `payipa/` 下：
```bash
uv sync --all-packages
uv run alembic -c deploy/alembic.ini upgrade head     # 首次建表（三库）
```

## 1. 建管理员（首次，无自助注册）

```bash
uv run pyp-admin create-user admin
```

## 2. 起主控 + 一个 agent（两个终端，均在 `payipa/`）

```bash
# 终端 1：主控
uv run uvicorn pyp_server.main:app --host 127.0.0.1 --port 8000
# 终端 2：一个采集 agent（出站 WS 连主控）
uv run pyp-agent join --server http://127.0.0.1:8000 --token dev
```

## 3. 浏览器测试（推荐路径）

1. 打开 http://127.0.0.1:8000/ → 自动跳登录 → 用 `admin` + 你的密码 登录。
2. 「＋ 新建数据源」→ 表单已预填 books.toscrape.com 示例：填**短码**（如 `books`）→「创建并运行」。
3. 自动跳到 `/data/books`，Tabulator 表格看到抓取结果（表头筛选 / 点列头排序 / 翻页均走服务端）。

## 4. Docker Agent

Agent 容器出站连接主控，不开放入站端口。镜像构建上下文是 payipa 仓库根，依赖由 `uv.lock` 固定解析。

```bash
# 0) 主控绑 0.0.0.0，容器才连得上（在 payipa/ 下）
uv run uvicorn pyp_server.main:app --host 0.0.0.0 --port 8000

# 1) 登录主控，在「节点管理 → 添加节点」生成一次性入网码

# 2) 启动一个 Agent；Compose project 的 named volume 会持久化节点身份
PYP_SERVER=http://host.docker.internal:8000 PYP_TOKEN='<一次性入网码>' \
docker compose -p pyp-agent-local -f deploy/docker-compose.agents.yml up --build -d

# 3) 看在线节点
curl -s http://127.0.0.1:8000/api/agents

# 停：
docker compose -p pyp-agent-local -f deploy/docker-compose.agents.yml down
```

不要用 `--scale` 共享同一个身份。多个 Agent 应逐个签发入网码，并使用不同主机或不同 Compose project。需要浏览器能力时设置 `PYP_AGENT_DOCKERFILE=packages/pyp-agent/Dockerfile.browser`。

派发由**后台派发环**负责：请求先以 QUEUED 落库，主控每秒把排队请求铺到各在线容器的空闲槽（跨容器公平铺满）。
所以**请求数超过总槽数也不会丢**——多的排队、随空闲槽腾出陆续下发；某容器中途挂掉，它的在途请求自动回队重排到存活容器（`/api/agents` 的 `slot_used`/`inflight` 会随之变化）。抓完数据进 `/data/{短码}`。
进度可观测：`GET /api/monitor/batches/{batch_id}`（按 state 实时聚合 total/ok/fail/running/pct）、`GET /api/monitor/queue`（排队深度）。

## 也可用 API 触发已确认的数据源

```bash
curl -X POST "http://127.0.0.1:8000/api/sources/books/run" \
  -H "content-type: application/json" --data @deploy/examples/books.json
# 看进度（batch_id 取上一步返回值）
curl "http://127.0.0.1:8000/api/monitor/batches/<batch_id>"
```
> 返回里 `dispatched` 恒为 0：派发不在建批次时同步发生，改由后台环负责（见上）。
> API 运行只接受已经留存访问依据且未暂停的数据源；首次创建请使用 `/sources/new`，或由管理员先完成访问复核。
> 页面（`/data`、`/sources`）需登录；JSON API 的 RBAC 权限闸门默认关（`PYP_SERVER_RBAC_ENABLED=false`，保持开放便于本地起步）。
> 生产启用：首次 `/setup` 会自动播种默认角色并赋首个管理员；其他用户可用 `uv run pyp-admin grant-role <用户> <角色>` 授权。Compose 部署在 `deploy/.env.compose` 置 `PYP_SERVER_RBAC_ENABLED=true`；本地开发路径则使用 `.env`。

## 还不能测（后续里程碑）

- **任务调度进阶（M2 后续切片）**：cron/定时触发、Redis 队列、优先级排序、限流/自动调频、Cancel 取消、分组亲和、权重均衡。当前已具备：**持久化队列 + 自动派发环 + 租约回收 + 断连重排 + 监控端点实时聚合**。
- **组装 / 推送 / 对外 Dataset API**（M3–M4，已具备）；**AI/LLM Gateway**（M5，已具备——`llm.manage` 权限，管理员登记模型/凭证 KEK 加密，`POST /api/llm/complete` 经 Gateway 调模型、成本挂 task_id 记审计；本地无 key 用 `provider=echo` 冒烟）。
- **访问边界**（M5，已具备——新源留存访问依据；401/403/451 在解析和归档前暂停整源；调度停止；页面或 `POST /api/sources/{uuid}/access-review` 人工复核后恢复）。网络路径由部署环境固定配置。
- **浏览器采集**（M5，agent 标准 Playwright 引擎已实现——`engine_hint=browser` 起 headless chromium 渲染；agent 装 `[browser]` extra + `playwright install chromium` 后自动上报 automation 能力、主控按能力分组派发。真浏览器冒烟需浏览器运行时）。
- **镜像发布**：`docker pull` / `pip install pyp-agent` 尚未发到 registry/PyPI（现为本地 `docker build`，见 §4）。
- **RBAC 管理界面**：权限矩阵（角色×资源×动作）已落库生效（M5，`payipa.security.rbac` + `require_perm` 闸门，开关 `PYP_SERVER_RBAC_ENABLED`）；用户/角色的**页面化管理**（现走 `pyp-admin` CLI）归 06 界面后续轮。
