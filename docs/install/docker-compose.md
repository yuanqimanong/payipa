# 安装 · Docker Compose 一键路径（P0-02/03）

> 适用：All-in-One 档位（试用/小团队/验收，见 13 §2.2）。**v1 硬约束：单主控实例、单 uvicorn worker**。
> 正式支持 Linux x86_64 主控；Windows + Docker Desktop/WSL2 可用于本地验证（本文命令两端通用）。

## 前置

- Docker ≥ 24 与 Docker Compose v2（`docker compose version` 可用）。
- 双仓兄弟目录布局（构建上下文 = `project/` 根）：

  ```text
  project/
    payipa/            # 本仓库
    jianbing_utils/    # 兄弟仓（镜像从双仓源码打 wheel，无 git/apt 依赖）
  ```

- 一次性装好 CLI（`pypctl` 随 pyp-server 包发布）：在 `payipa/` 下 `uv sync --all-packages`。

## 三条命令

在 `payipa/` 仓库根执行：

```bash
uv run pypctl init          # 1) 生成 deploy/.env.compose（各密钥独立随机；已存在拒绝覆盖）
uv run pypctl up --build    # 2) 构建镜像并按依赖顺序启动：db → migrate(one-shot) → server，等到 readyz 绿
uv run pypctl smoke         # 3) 冒烟门：readyz 全绿 + 三库 revision 一致
```

然后浏览器打开 **http://127.0.0.1:8100/setup** 创建首个管理员（空库首启引导），即完成安装。

辅助命令：`pypctl doctor`（环境体检）、`pypctl status`（livez/readyz/version 汇总）、
`pypctl down`（停；`-v` 连数据卷删掉重来）。不想用 CLI 时等价的裸命令：

```bash
docker build -f payipa/deploy/Dockerfile.server -t payipa-server:local .   # 在 project/ 根
docker compose -f deploy/compose.yml --env-file deploy/.env.compose up -d  # 在 payipa/ 下
```

## 端口与卷

| 对象 | 说明 |
|---|---|
| `server` 端口 | 宿主 **8100** → 容器 8000（8000 常被本机 dev 主控占用） |
| `db` 端口 | **不映射宿主端口**——服务间内网互联即可，避免与本机已有 PostgreSQL 的 5432 冲突 |
| 卷 `pgdata` | PostgreSQL 数据（三库 `pyp_sys` / `data_center` / `business`，后两库由 `deploy/initdb/01-create-dbs.sql` 首次初始化补建） |
| 卷 `pyp_data` | 主控本地对象存储根（容器内 `/data`，即 `DATA_ROOT`） |
| 卷 `agent_state` | agent 节点身份持久卷（容器重建节点身份不变） |

迁移是显式 one-shot job（`migrate` 服务，`alembic upgrade heads`）：server 只在 migrate 成功后启动，
自身**不会**抢跑迁移；三库不到 head 时 `/readyz` 返回 503，编排器不会放流量。

## 可选：带采集节点

```bash
uv run pypctl up --agents            # 或 docker compose ... --profile agents up -d --scale agent=3
```

agent 走内网 `http://server:8000` 接入，join token 取 `.env.compose` 里 `PYP_SERVER_AGENT_JOIN_TOKEN`。

## 生产必改项

`pypctl init` 生成的密钥已经是独立随机值，但默认 `PYP_SERVER_ENVIRONMENT=dev`（宽松：JSON API 免登录）。
**生产上线前必须**在 `deploy/.env.compose` 改：

1. `PYP_SERVER_ENVIRONMENT=production` —— 启动前置校验（preflight）拒绝一切 dev 默认密钥；
2. `PYP_SERVER_RBAC_ENABLED=true` —— production 模式强制（否则开机即失败）；
3. 复核各密钥为独立随机长串（≥32 字节）：`PG_PASSWORD` / `PYP_SERVER_SESSION_SECRET` / `UPLOAD_SECRET` / `CRED_KEK` / `PYP_SERVER_AGENT_JOIN_TOKEN`；
4. 基础镜像按 digest 固定（`deploy/Dockerfile.server` 头注释）并以 `--build-arg BUILD_COMMIT=$(git rev-parse --short HEAD)` 注入版本指纹；
5. 反向代理（TLS + WebSocket 长连接超时）自备——compose 未内置。

## 诚实边界（当前版本）

- `pypctl smoke` 只验「可服务 + 迁移一致」；**内置 fixture 全链路采集冒烟**随首启向导（P0-21）补齐。
- 存储仅本地卷（S3 未实现，配置 `S3_*` 会拒绝启动）；Redis 未接线。
- agent 仍用共享 join token 首次入网（一次性入网码归 P0-07）。
