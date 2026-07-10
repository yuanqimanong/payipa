"""真 SandboxExecutor（M3 slice-6，04A 定案）：Linux 容器隔离执行组装脚本。

**Windows 无独立 Linux 机器的替代方案（决策 2026-07-11）**：Docker Desktop 的 WSL2 后端即真
Linux 容器宿主（linux/amd64 + cgroup v2 + runc），04A 要求的全部隔离原语可用；Linux 服务器上
行为完全一致（探测的是 docker server 的 OS，不是宿主 OS）。无 Docker / 非 Linux server 时
`sandbox_available()` 为 False，调用方降级 LocalExecutor（04A：仅开发/受信场景）。

隔离面（04A「锁定容器 + 断网 + egress 仅 Gateway」）：
- 容器：``--read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit --memory
  --cpus --user nobody --tmpfs /tmp``；脚本/输入只读挂载 /job，结果经 /out 卷回传
  （stdout 只算日志，用户 print 不污染协议）。
- 网络：internal 网络 ``payipa-sandbox-egress``（无外网路由）+ 路径白名单代理
  ``payipa-sandbox-gw``（nginx，仅放行 ``POST /internal/query`` → host.docker.internal:主控端口，
  其余一律 403；代理另一腿挂 bridge 出主机）。沙箱容器唯一可达 = 该代理。
- 数据面：容器只持有 job_token（scope=表白名单 + 行配额，租约=执行超时），零 DB/S3/KEK 凭证
  （与 M4 推送子进程同思路，但这里是内核级隔离而非应用级白名单）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

import anyio

from payipa.crawl.ingest import data_table_name
from payipa.security.job_token import issue_job_token

SANDBOX_NETWORK = "payipa-sandbox-egress"
GATEWAY_PROXY_NAME = "payipa-sandbox-gw"
GATEWAY_ALIAS = "payipa-gw"  # 沙箱容器内可达的唯一主机名
GATEWAY_PORT_LABEL = "payipa.gateway-port"
DEFAULT_IMAGE = "python:3.14-slim"
PROXY_IMAGE = "nginx:alpine"
_MAX_LOG_BYTES = 64 * 1024


class SandboxUnavailable(RuntimeError):
    """本机没有可用的 Linux 容器运行时（无 docker 或 server 非 linux）。"""


class SandboxTimeout(RuntimeError):
    """组装脚本超出墙钟时限，容器已被杀。"""


class SandboxScriptError(RuntimeError):
    """组装脚本在沙箱内失败（语法错/运行时异常/越权 403 等），携带脚本侧错误与容器日志。"""

    def __init__(self, error: str, logs: str = "") -> None:
        super().__init__(error)
        self.logs = logs


@lru_cache(maxsize=1)
def sandbox_available(docker: str = "docker") -> bool:
    """探测 Linux 容器运行时：docker CLI 在且 server OS 为 linux（Docker Desktop/WSL2 与 Linux 宿主同判）。"""
    try:
        out = subprocess.run(  # noqa: S603 —— 固定参数探测，非用户输入
            [docker, "version", "--format", "{{.Server.Os}}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return out.returncode == 0 and out.stdout.decode().strip() == "linux"


def gateway_proxy_conf(gateway_port: int) -> str:
    """egress 代理的 nginx 配置：仅放行 Query Gateway 一个路径，其余 403（04A 白名单口径）。"""
    return (
        "server {\n"
        "  listen 80;\n"
        "  location = /internal/query {\n"
        f"    proxy_pass http://host.docker.internal:{gateway_port};\n"
        "    proxy_http_version 1.1;\n"
        '    proxy_set_header Host "$host";\n'
        "  }\n"
        "  location / { return 403; }\n"
        "}\n"
    )


async def _docker(*args: str, docker: str = "docker", input_: bytes | None = None):
    return await anyio.run_process([docker, *args], input=input_, check=False)


async def _ensure_image(image: str, *, docker: str = "docker") -> None:
    """镜像不在本地则先拉取——拉取耗时不能算进脚本执行超时。"""
    probe = await _docker("image", "inspect", image, docker=docker)
    if probe.returncode != 0:
        pull = await _docker("pull", image, docker=docker)
        if pull.returncode != 0:
            raise SandboxUnavailable(f"镜像拉取失败 {image}: {pull.stderr.decode(errors='replace')[:300]}")


async def ensure_sandbox_infra(gateway_port: int, *, docker: str = "docker") -> None:
    """幂等保障沙箱基础设施：internal 网络 + egress 代理容器（端口变更自动重建）。

    代理配置经环境变量注入（容器启动时落盘），不依赖宿主机文件——重启机器后 `--restart
    unless-stopped` 自愈，无挂载路径漂移问题。
    """
    if not sandbox_available(docker):
        raise SandboxUnavailable("没有可用的 Linux 容器运行时（需 Docker Desktop/WSL2 或 Linux docker 宿主）")
    await _ensure_image(PROXY_IMAGE, docker=docker)
    net = await _docker("network", "inspect", SANDBOX_NETWORK, docker=docker)
    if net.returncode != 0:
        create = await _docker("network", "create", "--internal", SANDBOX_NETWORK, docker=docker)
        if create.returncode != 0:
            raise SandboxUnavailable(f"沙箱网络创建失败: {create.stderr.decode(errors='replace')[:300]}")

    want_port = str(gateway_port)
    probe = await _docker(
        "inspect",
        "--format",
        f'{{{{.State.Running}}}} {{{{index .Config.Labels "{GATEWAY_PORT_LABEL}"}}}}',
        GATEWAY_PROXY_NAME,
        docker=docker,
    )
    if probe.returncode == 0 and probe.stdout.decode().strip() == f"true {want_port}":
        return  # 代理已就绪且指向正确端口
    await _docker("rm", "-f", GATEWAY_PROXY_NAME, docker=docker)  # 不存在时报错忽略
    run = await _docker(
        "run",
        "-d",
        "--name",
        GATEWAY_PROXY_NAME,
        "--label",
        f"{GATEWAY_PORT_LABEL}={want_port}",
        "--restart",
        "unless-stopped",
        "--network",
        SANDBOX_NETWORK,
        "--network-alias",
        GATEWAY_ALIAS,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-e",
        f"PYP_GW_CONF={gateway_proxy_conf(gateway_port)}",
        PROXY_IMAGE,
        "sh",
        "-c",
        'printf "%s" "$PYP_GW_CONF" > /etc/nginx/conf.d/default.conf && exec nginx -g "daemon off;"',
        docker=docker,
    )
    if run.returncode != 0:
        raise SandboxUnavailable(f"egress 代理启动失败: {run.stderr.decode(errors='replace')[:300]}")
    # 代理的出主机腿：连 bridge（已连报错忽略）
    await _docker("network", "connect", "bridge", GATEWAY_PROXY_NAME, docker=docker)


class SandboxExecutor:
    """在锁定 Linux 容器里执行组装脚本源码，经 egress 代理走 Query Gateway 取数。

    与 LocalExecutor 的分工：Local 拿**可调用对象**进程内跑（受信降级）；Sandbox 拿**脚本源码**
    容器内跑（生产口径）。两者的脚本约定一致（固定入口 ``assemble(ctx)``、ctx.read_table 同签名）。
    """

    def __init__(
        self,
        secret: str,
        *,
        gateway_port: int = 8000,
        image: str = DEFAULT_IMAGE,
        memory: str = "512m",
        cpus: str = "1.0",
        pids_limit: int = 256,
        timeout_s: float = 120.0,
        page_limit: int = 500,
        docker: str = "docker",
    ) -> None:
        self._secret = secret
        self._gateway_port = gateway_port
        self._image = image
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._timeout_s = timeout_s
        self._page_limit = page_limit
        self._docker = docker

    async def run_source(
        self,
        script_source: str,
        *,
        job_id: str,
        sources: list[str],
        row_quota: int | None = None,
        watermarks: dict[str, int] | None = None,
    ) -> tuple[list[dict], dict[str, int]]:
        """跑一份组装脚本源码，返回 (产物行, 各源新水位)。失败抛 SandboxScriptError/SandboxTimeout。"""
        await ensure_sandbox_infra(self._gateway_port, docker=self._docker)
        token, _jti = issue_job_token(
            self._secret,
            job_id,
            tables=[data_table_name(s) for s in sources],
            row_quota=row_quota,
            lease_s=int(self._timeout_s) + 60,
        )
        spec = {
            "source": script_source,
            "entry": "assemble",
            "gateway_url": f"http://{GATEWAY_ALIAS}/internal/query",
            "job_token": token,
            "watermarks": dict(watermarks or {}),
            "page_limit": self._page_limit,
        }
        await _ensure_image(self._image, docker=self._docker)
        workdir = Path(tempfile.mkdtemp(prefix="pyp-sandbox-"))
        name = f"pyp-sbx-{re.sub(r'[^a-zA-Z0-9_.-]', '-', job_id)[:40]}-{uuid.uuid4().hex[:6]}"
        try:
            jobdir, outdir = workdir / "job", workdir / "out"
            jobdir.mkdir()
            outdir.mkdir()
            (jobdir / "child.py").write_text(
                Path(__file__).with_name("_sandbox_child.py").read_text(encoding="utf-8"), encoding="utf-8"
            )
            # 容器内以 nobody(65534) 运行：原生 Linux 上挂载目录须对其可读（/job）可写（/out）；
            # mkdtemp 默认 0700 会挡住。Windows(Docker Desktop) 挂载权限是虚拟的，chmod 无害。
            workdir.chmod(0o755)
            jobdir.chmod(0o755)
            (jobdir / "child.py").chmod(0o644)
            outdir.chmod(0o777)
            cmd = [
                self._docker, "run", "--rm", "-i",
                "--name", name,
                "--network", SANDBOX_NETWORK,
                "--read-only",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--pids-limit", str(self._pids_limit),
                "--memory", self._memory,
                "--memory-swap", self._memory,  # 禁 swap 逃逸内存限额
                "--cpus", self._cpus,
                "--user", "65534:65534",  # nobody
                "--tmpfs", "/tmp:rw,size=64m",
                "-v", f"{jobdir}:/job:ro",
                "-v", f"{outdir}:/out",
                self._image,
                "python", "/job/child.py",
            ]  # fmt: skip
            try:
                with anyio.fail_after(self._timeout_s):
                    proc = await anyio.run_process(cmd, input=json.dumps(spec).encode(), check=False)
            except TimeoutError:
                await _docker("kill", name, docker=self._docker)  # 杀容器（杀 CLI 不等于杀容器）
                raise SandboxTimeout(f"组装脚本超时（>{self._timeout_s}s），容器已终止") from None
            logs = (proc.stdout + proc.stderr).decode(errors="replace")[-_MAX_LOG_BYTES:]
            result_path = outdir / "result.json"
            if not result_path.exists():
                raise SandboxScriptError(f"容器未产出结果（exit={proc.returncode}）", logs)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not result.get("ok"):
                raise SandboxScriptError(str(result.get("error", "unknown error")), logs)
            return result["rows"], {k: int(v) for k, v in (result.get("new_watermarks") or {}).items()}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
