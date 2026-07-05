# pyp-agent 部署镜像（M0 骨架）。
# 发布形态 = PyPI 包 + Docker 镜像（决策记录 01）。依赖刻意最小、零 DB/S3 密钥；
# 出站 WS 连接，不监听端口；并发靠槽位 N（非多进程，一机一 agent，09 定案）。
FROM python:3.14-slim

# 钉 uv 版本（官方镜像 copy 模式）
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

WORKDIR /app

# M0：contracts/jb 发布 PyPI 后 `uv pip install --system pyp-agent` 即可。
# 联调期改为 COPY 本仓源码 + `uv pip install --system .`（workspace 成员）。
RUN uv pip install --system pyp-agent || echo "M0 骨架：pyp-agent 尚未发布 PyPI，构建期跳过安装"

ENTRYPOINT ["pyp-agent"]
CMD ["--help"]
