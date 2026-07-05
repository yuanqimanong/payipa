# QUICKSTART —— 本地手动测试（M1 + 登录 + 建源界面）

> 现可用：**登录 → 建源界面 → 采集 → 查看页（Tabulator）** 全流程。
> 尚未做（见文末）：任务调度/多节点、组装/推送、RBAC 全量 API 鉴权、agent 打包分发。

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

## 也可用 API（curl，无需登录）

```bash
curl -X POST "http://127.0.0.1:8000/api/sources/books/run" \
  -H "content-type: application/json" --data @deploy/examples/books.json
```
> 页面（`/data`、`/sources`）需登录；JSON API 目前开放，细粒度 RBAC 鉴权在后续安全里程碑接入。

## 还不能测（后续里程碑）

- **任务调度**（cron/定时/API 触发）、Redis 队列、多 agent 派发、限流调频、租约回收（M2）。
- **组装 / 推送 / 对外 Dataset API / AI 帮写 / 代理中转 / 反检测引擎**（M3–M5）。
- **agent 分发**：`pip install pyp-agent` / `docker pull` 尚未发布（现只能在本仓 workspace 内跑 agent）。
- **完整权限（RBAC）**：现仅"登录"门槛 + 页面保护；角色×资源×动作矩阵在 06/09 里程碑。
