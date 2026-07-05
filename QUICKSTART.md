# QUICKSTART —— 本地手动测试（M1 + 登录 + 建源界面）

> 现可用：**登录 → 建源界面 → 采集 → 查看页（Tabulator）** 全流程，agent 可**多容器**运行（见 §4）。
> 尚未做（见文末）：任务调度/队列、组装/推送、RBAC 全量 API 鉴权、镜像发布到 registry。

## 前置

PostgreSQL 已起（Docker，连接在 `../.env`）；在 `payipa/` 下：
```bash
uv sync --all-packages
uv run alembic -c deploy/alembic.ini upgrade head     # 首次建表（三库）
```

## 1. 建管理员（首次，无自助注册）

```bash
uv run pyp-admin create-user admin <你的密码>
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

## 4. Docker 多节点（多容器测「上线」）

agent 可打成镜像、跑**多个容器 = 多个采集节点**（每容器 agent-id 默认取容器主机名，天然唯一，不撞车）。
主控（uvicorn）仍在**宿主机** `:8000`；agent 容器出站连 `host.docker.internal`。

> 构建上下文 = `project/` 根（含 `payipa` 与 `jianbing_utils` 兄弟目录），镜像从**双仓源码**构建，不依赖 git/apt。

```bash
# 0) 主控绑 0.0.0.0，容器才连得上（在 payipa/ 下）
uv run uvicorn pyp_server.main:app --host 0.0.0.0 --port 8000

# 1) 起 3 个 agent 容器（在 payipa/ 下；首次自动 build）
docker compose -f deploy/docker-compose.agents.yml up --build --scale agent=3

# 2) 看在线节点（应出现 3 个 agent）
curl -s http://127.0.0.1:8000/api/agents

# 停：
docker compose -f deploy/docker-compose.agents.yml down
```

自定义主控地址/令牌：`PYP_SERVER=http://host.docker.internal:8000 PYP_TOKEN=dev docker compose ... up --scale agent=5`。
单个容器（不用 compose）：在 `project/` 根 `docker build -f payipa/packages/pyp-agent/Dockerfile -t payipa-agent:local .`，
再 `docker run --rm --add-host host.docker.internal:host-gateway payipa-agent:local join --server http://host.docker.internal:8000 --token dev`。

派发的任务会落到某个空闲容器（`/api/agents` 里该 agent 的 `slot_used`/`inflight` 会变化），抓完数据同样进 `/data/{短码}`。

## 也可用 API（curl，无需登录）

```bash
curl -X POST "http://127.0.0.1:8000/api/sources/books/run" \
  -H "content-type: application/json" --data @deploy/examples/books.json
```
> 页面（`/data`、`/sources`）需登录；JSON API 目前开放，细粒度 RBAC 鉴权在后续安全里程碑接入。

## 还不能测（后续里程碑）

- **任务调度**（cron/定时/API 触发）、Redis 队列、限流调频、租约回收（M2）。多 agent **已可派发**（空闲槽轮转），但队列/重派/负载均衡策略待 M2。
- **组装 / 推送 / 对外 Dataset API / AI 帮写 / 代理中转 / 反检测引擎**（M3–M5）。
- **镜像发布**：`docker pull` / `pip install pyp-agent` 尚未发到 registry/PyPI（现为本地 `docker build`，见 §4）。
- **完整权限（RBAC）**：现仅"登录"门槛 + 页面保护；角色×资源×动作矩阵在 06/09 里程碑。
