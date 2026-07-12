# Docker Compose 安装与验收

这是容器化部署的唯一操作手册。它覆盖主控、三库、首个管理员、Agent 和上线验收；`QUICKSTART.md` 的后续章节用于本地开发和断点调试。

v1 的边界是单主控、单 Uvicorn worker、本地持久卷和任意数量的出站 Agent。主控启动顺序固定为 `db -> migrate -> server`：三个数据库全部建好且迁移成功后，主控才会启动。

## 安装前确认

- Linux x86_64 生产主机，或 Docker Desktop/WSL2 验证环境。
- Docker 24+ 与 Docker Compose v2。
- Python 3.14 与 uv 0.11.x，仅用于运行 `pypctl`。按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装即可。
- 首次构建必须能访问 Docker Hub、GHCR、GitHub 和 Python 包索引；`uv.lock` 固定了依赖版本，但仍需要从 GitHub 拉取固定 tag 的 `jianbing-utils`。离线环境应先镜像/缓存这些依赖。
- 主控建议至少预留 10 GB 磁盘；Browser Agent 还需要 Chromium 的镜像空间和内存。

以下命令都在 `payipa` 仓库根目录执行。代码中的镜像构建上下文就是当前仓库，不依赖兄弟仓库。

> 代码块采用 Bash 写法。PowerShell 中，环境变量前缀改为 `$env:NAME='value';` 后再执行同一条 Docker 命令。

## 本地试用

本地试用生成 `dev` 配置，主控只绑定 `127.0.0.1:8100`，适合在本机完成产品验收；不要把该模式直接暴露到公网。

```bash
uv sync --package pyp-server --locked
uv run pypctl init
uv run pypctl doctor
uv run pypctl up --build
uv run pypctl smoke
```

`doctor` 会检查 Docker、Compose、配置、端口和 Compose 语法。`up --build` 会等待数据库健康、one-shot 迁移和主控健康检查；成功后访问 `http://127.0.0.1:8100/setup`。

## 生产主控

生产环境从一开始就生成严格配置。把示例域名替换为实际访问主控的域名或 IP，**不要**带 `https://`、端口或路径。

```bash
uv sync --package pyp-server --locked
uv run pypctl init --production-host pyp.example.com
uv run pypctl doctor
uv run pypctl up --build
uv run pypctl smoke
```

已有 `deploy/.env.compose` 时，`pypctl init` 会拒绝覆盖，避免意外轮换数据库密码、会话密钥和 KEK。升级或把既有试用环境切到生产时，应先备份该文件，再手工设置 `PYP_SERVER_ENVIRONMENT=production`、`PYP_SERVER_RBAC_ENABLED=true`，并新增或更新 `PYP_SERVER_ALLOWED_HOSTS` 为实际对外域名/IP；随后重新执行 `uv run pypctl doctor`。不要为了切换生产模式删除已有配置或数据卷。

该命令会生成独立随机密钥，并设置：

```dotenv
PYP_SERVER_ENVIRONMENT=production
PYP_SERVER_RBAC_ENABLED=true
PYP_SERVER_ALLOWED_HOSTS=localhost,127.0.0.1,server,pyp.example.com
PYP_DEPLOY_BIND_ADDRESS=127.0.0.1
```

`pypctl doctor` 会在启动前拦截模板占位符、短密钥、重复三库名、错误环境名、生产 RBAC 未开启和通配 Host。服务启动时还会执行同一类生产前置校验，任何一项不满足都会拒绝启动。

### TLS 与反向代理

Compose 故意只监听本机 `127.0.0.1:8100`。生产环境应由同一主机上的 TLS 反向代理转发，不应直接将开发端口裸露到公网。以 Caddy 为例，DNS 已指向主机且 80/443 可达时，Caddyfile 可写为：

```caddyfile
pyp.example.com {
    reverse_proxy 127.0.0.1:8100
}
```

Caddy 会处理 HTTPS 和 WebSocket Upgrade；若使用 Nginx、云负载均衡或 Ingress，需保留原始 `Host`、转发 WebSocket Upgrade，并把上传体积上限设为不低于 `PYP_SERVER_MAX_UPLOAD_MB`。代理生效后可分别检查本机和对外地址：

```bash
curl -fsS http://127.0.0.1:8100/readyz
curl -fsS https://pyp.example.com/readyz
```

需要受控内网直连时，才把 `PYP_DEPLOY_BIND_ADDRESS` 改为 `0.0.0.0` 并配置防火墙、TLS 和访问控制；`doctor` 会给出明确警告。

### 三库名称与配置保管

`PG_DB_PYP`、`PG_DB_DATA_CENTER`、`PG_DB_BUSINESS` 可以在**首次**启动前改名，三者必须不同。PostgreSQL 的初始化脚本会按这三个变量创建数据库；数据卷一旦初始化，之后不能只改 `.env.compose` 中的库名。改名需做受控迁移，或在确认无数据时执行 `uv run pypctl down --volumes` 后重新初始化。

`deploy/.env.compose` 包含数据库密码、会话密钥、安装码和凭证 KEK。Linux 上 `pypctl init` 会将其设为仅属主可读；仍应将其纳入密钥管理和备份访问控制，不能提交到 Git。

## 创建首个管理员

在空库首次启动后打开以下页面：

- 本地试用：`http://127.0.0.1:8100/setup`
- 生产：`https://pyp.example.com/setup`

从本机受保护的 `deploy/.env.compose` 读取 `PYP_SERVER_BOOTSTRAP_TOKEN`，在页面中创建管理员账号。安装码只用于空库首个管理员，不是登录密码；首个账号会自动播种默认 RBAC 角色并获得管理员权限。创建完成后页面会转到登录页。

## 接入 Agent

Agent 始终主动通过 WebSocket 连接主控，不需要为 Agent 开放入站端口。先登录主控，进入“节点管理 -> 添加节点”，签发一个十分钟有效且仅能使用一次的入网码。

主控同机的 HTTP Agent 使用隐藏输入，避免把入网码写进 shell 历史：

```bash
uv run pypctl agent --build
```

按提示粘贴一次性入网码即可。自动化场景可改用 `--token-stdin`；`--token` 仅为兼容脚本保留，不推荐交互使用。

独立节点机需要有同一发布版本的 `payipa` 仓库副本和 Docker。当前 Agent 镜像尚未发布到镜像仓库，因此节点机首次接入会在本机构建镜像：

```bash
cd payipa
PYP_SERVER=https://pyp.example.com \
PYP_TOKEN='<节点页签发的一次性入网码>' \
docker compose -p payipa-agent-a -f deploy/docker-compose.agents.yml up --build -d
```

`payipa-agent-a` 是一个逻辑节点的 Compose project 名；它会获得独立的 Docker named volume 保存长期节点凭证。不要使用 `--scale` 复制同一个 Agent 服务。需要第二个 Agent 时，逐个签发新入网码，并使用另一个 project 名，例如 `payipa-agent-b`。

需要浏览器渲染能力时，在同一条命令前加上 Browser Dockerfile：

```bash
PYP_AGENT_DOCKERFILE=packages/pyp-agent/Dockerfile.browser \
PYP_SERVER=https://pyp.example.com \
PYP_TOKEN='<节点页签发的一次性入网码>' \
docker compose -p payipa-agent-browser -f deploy/docker-compose.agents.yml up --build -d
```

在节点机排障或停止该 Agent：

```bash
docker compose -p payipa-agent-a -f deploy/docker-compose.agents.yml logs --tail=100 agent
docker compose -p payipa-agent-a -f deploy/docker-compose.agents.yml down
```

`down` 不删除节点身份卷；只要该卷仍在，重启会使用长期节点凭证。若管理员撤销节点或删除该卷，必须重新签发入网码。

## 首次验收

`pypctl smoke` 只验证主控可服务、三个数据库迁移一致；它不等同于真实采集验收。首次安装完成后按以下顺序检查：

1. 执行 `uv run pypctl status`，确认 `livez`、`readyz` 都返回 `200`，且三个 schema revision 等于同一个 `expected_head`。
2. 登录主控，确认仪表盘和节点管理页面可打开。
3. 确认新接入的 Agent 状态为在线；若未在线，先查看节点机的 Agent 日志。
4. 在“新建数据源”页面使用预填的 `books.toscrape.com` 示例，填写符合规则的短码（如 `books`），创建并运行。
5. 批次完成后打开 `/data/books`，确认数据表中出现采集结果。

完成这五项才代表“主控、控制面、Agent、采集和入库”链路均可用。

## 日常运维

```bash
uv run pypctl status
uv run pypctl smoke
uv run pypctl backup
uv run pypctl upgrade --build
uv run pypctl restore backups/20260712T120000Z --confirm RESTORE
uv run pypctl down
```

`backup` 会短暂停止主控，冷备三库、对象卷和部署配置，并写入 SHA-256 清单；备份含业务数据和密钥，必须加密保管。`restore` 会覆盖当前数据并校验 `CRED_KEK`，失败时主控保持停止。详细操作见 [备份恢复](../admin/backup-restore.md) 与 [升级回滚](../admin/upgrade-rollback.md)。

只有在确定要清空全部本地数据、从零重装时才执行：

```bash
uv run pypctl down --volumes
```

## 故障定位

| 现象 | 先做什么 |
|---|---|
| `doctor` 失败 | 按 `[!!]` 的项目修复；先确认是在仓库根目录执行，且 `.env.compose` 不是模板占位符。 |
| `up` 卡在迁移 | `docker compose -f deploy/compose.yml --env-file deploy/.env.compose logs migrate`；不要手工跳过迁移。 |
| `readyz` 非 200 | 执行 `uv run pypctl status`，再查看 `server` 日志；它会逐项报告三库、迁移、存储或后台循环。 |
| 生产启动即退出 | 检查 `PYP_SERVER_ENVIRONMENT`、RBAC、Allowed Hosts 和密钥；生产 preflight 会明确打印拒绝原因。 |
| Agent 未在线 | 检查 Agent 的 `PYP_SERVER` 是否是可访问的 HTTPS 地址、入网码是否过期或已使用，并查看 `agent` 日志。 |
| 改了数据库名称后起不来 | 数据卷已初始化时不能只改库名；恢复原配置，或按“三库名称与配置保管”一节做迁移/重装。 |

当前正式边界是本地对象卷、单主控和单 worker。S3、Redis 和多主控高可用尚未实现；配置 `S3_*` 或 `REDIS_URL` 会被启动前校验拒绝。
