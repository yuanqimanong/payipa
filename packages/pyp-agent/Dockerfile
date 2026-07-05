# pyp-agent 部署镜像（多阶段；从双仓源码构建，无 apt/git 依赖）。
# 构建上下文 = project/ 根（含 payipa 与 jianbing_utils 兄弟目录）——见根 .dockerignore。
#   在 project/ 根执行：
#     docker build -f payipa/packages/pyp-agent/Dockerfile -t payipa-agent:local .
#   运行单节点：
#     docker run --rm --add-host host.docker.internal:host-gateway payipa-agent:local \
#       join --server http://host.docker.internal:8000 --token dev
# agent 依赖刻意最小：contracts + jianbing-utils + niquests/websockets/anyio/parsel...；零 DB/S3 密钥。
# 出站 WS 连接、不监听端口；并发靠槽位 N（非多进程）。多容器 = 多节点（agent-id 默认取容器主机名，天然唯一）。

# ── build：uv 从源码打三个 wheel（contracts / jianbing_utils / pyp-agent）─────
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv
WORKDIR /w
COPY payipa/packages/payipa-contracts ./payipa-contracts
COPY payipa/packages/pyp-agent ./pyp-agent
COPY jianbing_utils ./jianbing_utils
RUN uv build --no-sources --out-dir /dist ./payipa-contracts \
 && uv build --no-sources --out-dir /dist ./jianbing_utils \
 && uv build --no-sources --out-dir /dist ./pyp-agent

# ── runtime：装三个本地 wheel（其余运行时依赖自 PyPI 解析）；无 git/apt ────────
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv
COPY --from=build /dist /dist
RUN uv pip install --system --no-cache /dist/*.whl && rm -rf /dist
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["pyp-agent"]
CMD ["--help"]
