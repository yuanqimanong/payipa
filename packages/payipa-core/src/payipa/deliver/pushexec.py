"""推送执行器（M4 slice-3b）：主控可信侧把推送组件丢进**隔离子进程**跑，只注入必要数据。

注入三样（决策 §4-4 / D57 / D59）：待推送行 rows、该组件**解密后**的目标凭证 creds、目标域白名单
allow_domains；子进程环境**擦洗**（剔除 PG_*/CRED_KEK/S3_* 等控制面秘密），无 DB / 无 KEK 句柄。
组件固定方法 ``push(ctx)`` 经 ``ctx.http`` 出网，越白名单即被拦（见 _push_child）。

本模块只在**可信父进程**运行；解密凭证由调用方（Consumer）用 KEK 完成后传入明文 dict，绝不把 KEK 传子进程。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio

_CHILD = str(Path(__file__).with_name("_push_child.py"))

# 传给子进程的白名单环境变量（其余一律剔除）。Windows 下 socket/SSL 需 SystemRoot；*nix 需 PATH。
_ENV_KEEP = ("PATH", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "SystemDrive", "TEMP", "TMP", "TMPDIR", "LANG", "TZ")


def _scrubbed_env() -> dict[str, str]:
    """最小化子进程环境：仅保留运行 Python/httpx 必需项，剔除一切控制面秘密。"""
    return {k: v for k, v in os.environ.items() if k in _ENV_KEEP}


class PushResult:
    __slots__ = ("error", "ok", "sent")

    def __init__(self, ok: bool, sent: int, error: str | None) -> None:
        self.ok = ok
        self.sent = sent
        self.error = error


async def run_push_component(
    code: str,
    rows: list[dict],
    *,
    creds: dict | None = None,
    allow_domains: list[str],
    entry: str = "push",
    timeout_s: float = 60.0,
) -> PushResult:
    """在隔离子进程执行推送组件；返回 PushResult(ok/sent/error)。子进程崩溃/超时 → ok=False。"""
    spec = json.dumps(
        {"code": code, "entry": entry, "rows": rows, "creds": creds or {}, "allow_domains": list(allow_domains)},
        ensure_ascii=False,
    )
    try:
        with anyio.fail_after(timeout_s):
            completed = await anyio.run_process(
                [sys.executable, _CHILD], input=spec.encode(), env=_scrubbed_env(), check=False
            )
    except TimeoutError:
        return PushResult(False, 0, f"push component timed out after {timeout_s}s")
    if completed.returncode != 0:
        err = (completed.stderr or b"").decode(errors="replace")[:2000]
        return PushResult(False, 0, f"child exited {completed.returncode}: {err}")
    try:
        out = json.loads(completed.stdout.decode() or "{}")
    except json.JSONDecodeError:
        return PushResult(False, 0, "child produced non-JSON output")
    return PushResult(bool(out.get("ok")), int(out.get("sent") or 0), out.get("error"))
