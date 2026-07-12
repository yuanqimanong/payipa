# Docker Compose 安装

适用范围：Linux x86_64 生产或 Docker Desktop/WSL2 验证。v1 固定为单主控、单 Uvicorn worker，可连接多台独立 Agent。

## 前置条件

- Docker 24+，Docker Compose v2。
- Python 3.14 与 uv 0.11.x，仅用于运行 `pypctl`。
- 只需克隆 payipa 本仓库。镜像从 `uv.lock` 解析固定版本的 `jianbing-utils`，不需要兄弟仓。
- 建议预留 10 GB 磁盘；Browser Agent 需要更多镜像空间和内存。

## 主控安装

在仓库根执行：

```bash
uv sync --package pyp-server --locked
uv run pypctl init
uv run pypctl doctor
uv run pypctl up --build
uv run pypctl smoke
```

启动顺序由 Compose 固定为 `db -> migrate -> server`：PostgreSQL 先创建 `pyp_sys`、`data_center`、`business`，迁移任务三库全部成功后才启动主控。访问 `http://127.0.0.1:8100/setup`，输入 `deploy/.env.compose` 中的 `PYP_SERVER_BOOTSTRAP_TOKEN` 创建首个管理员。

安装码只用于空库首个管理员，不能作为账号密码。生产部署后应限制 `.env.compose` 权限并纳入密钥托管。

## 接入 Agent

1. 登录主控，打开“节点管理 -> 添加节点”。
2. 页面生成一个十分钟有效、只能使用一次的入网码。
3. 在 Agent 机器执行页面给出的命令。入网后长期凭证保存在身份目录，后续重启不再需要入网码。

主控同机的单 HTTP Agent：

```bash
uv run pypctl agent --token '<一次性入网码>' --build
```

独立机器的 HTTP Agent：

```bash
PYP_SERVER=https://pyp.example.com \
PYP_TOKEN='<一次性入网码>' \
PYP_AGENT_STATE_DIR=/var/lib/payipa-agent \
docker compose -f deploy/docker-compose.agents.yml up --build -d
```

需要标准浏览器渲染能力时增加：

```bash
PYP_AGENT_DOCKERFILE=packages/pyp-agent/Dockerfile.browser
```

每台 Agent 必须使用独立且持久化的 `PYP_AGENT_STATE_DIR`。不要对同一个 Compose 服务使用 `--scale`，否则多个容器会共享节点身份。扩容应在不同主机或不同 Compose project 中逐个签发入网码。

## 生产配置

在 `deploy/.env.compose` 至少设置：

```dotenv
PYP_SERVER_ENVIRONMENT=production
PYP_SERVER_RBAC_ENABLED=true
PYP_SERVER_ALLOWED_HOSTS=localhost,127.0.0.1,server,pyp.example.com
```

`pypctl init` 已为 `PG_PASSWORD`、`PYP_SERVER_SESSION_SECRET`、`PYP_SERVER_BOOTSTRAP_TOKEN`、`UPLOAD_SECRET`、`CRED_KEK` 分别生成随机值。生产 preflight 会拒绝默认安装码、短会话密钥、默认上传密钥、默认 KEK、通配 Host 或未开启的 RBAC。生产只接受数据库签发的一次性 Agent 入网码，不接受共享 join token。

反向代理需配置 TLS、WebSocket Upgrade、长连接空闲超时、可信代理头和上传体积限制。当前 Compose 不自带公网 TLS。

## 运维命令

```bash
uv run pypctl status
uv run pypctl smoke
uv run pypctl backup
uv run pypctl upgrade --build
uv run pypctl restore backups/20260712T120000Z --confirm RESTORE
uv run pypctl down
```

`backup` 会短暂停止正在运行的主控，冷备三个数据库、`pyp_data` 对象卷和部署配置，并生成 SHA-256 清单。备份含数据库内容和密钥，必须加密保管。`restore` 是覆盖操作，会先验证清单和 `CRED_KEK`，恢复失败时保持主控停止。详细流程见 [备份恢复](../admin/backup-restore.md) 与 [升级回滚](../admin/upgrade-rollback.md)。

## 健康与故障定位

- `/livez`：进程存活。
- `/readyz`：三库、迁移、存储和后台循环可服务。
- `/version`：server/contracts/build commit 与三库 revision。
- `pypctl doctor`：Docker、Compose、配置、端口和已运行主控。
- `pypctl smoke`：readiness 与三库 revision 门禁；首次向导的示例采集验证 Agent 控制面和数据面。

当前正式边界：本地对象卷、单主控、单 worker。S3、Redis 和主控高可用尚未实现，配置 `S3_*` 或 `REDIS_URL` 会拒绝启动。
