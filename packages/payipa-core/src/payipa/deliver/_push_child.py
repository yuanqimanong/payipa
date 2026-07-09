"""推送组件隔离子进程 runner（M4 slice-3b）——**独立脚本，按路径执行**，故不触发 payipa 包 __init__。

父进程（主控可信侧）以 ``sys.executable <此文件>`` 拉起本进程，环境已擦洗（无 PG_*/CRED_KEK/S3 等）。
本进程从 stdin 读一份 JSON：``{code, entry, rows, creds, allow_domains}``，构造**受限 HTTP 客户端**
（仅放行 allow_domains 内主机；越界即 PushBlocked，请求根本不发出），exec 组件代码取固定方法 entry，
调用 ``entry(ctx)`` 完成投递，最后向 stdout 写 ``{ok, sent, error}``。

只依赖 stdlib + httpx；**不导入 payipa 包**（无 DB / 无 KEK / 无对象存储句柄）——隔离即容器。
真正的内核级出网封禁（仅放行目标域）与 04A 组装沙箱同属 Linux 加固项，本层为应用级白名单 + env 擦洗。
"""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlparse

import httpx


class PushBlocked(RuntimeError):
    """组件试图访问白名单外的主机——请求被拦下（未发出）。"""


def _host_allowed(host: str | None, allow: list[str]) -> bool:
    if not host:
        return False
    host = host.lower()
    # 精确或子域后缀匹配（"api.example.com" 命中 "example.com"）
    return any(host == d or host.endswith("." + d) for d in (a.lower().strip() for a in allow) if d)


class _WhitelistedHTTP:
    """注入组件的唯一出网句柄：每次请求先校验目标主机在白名单内，否则 PushBlocked。"""

    def __init__(self, allow_domains: list[str], *, timeout: float = 30.0) -> None:
        self._allow = allow_domains
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    def _check(self, url: str) -> None:
        if not _host_allowed(urlparse(url).hostname, self._allow):
            raise PushBlocked(f"target host not in whitelist: {url} (allow={self._allow})")

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self._check(url)
        return self._client.request(method, url, **kw)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("PUT", url, **kw)

    def close(self) -> None:
        self._client.close()


class _PushContext:
    """交给推送组件的上下文：待推送行 rows、解密后的目标凭证 creds、受限出网 http。无 DB、无 KEK。"""

    def __init__(self, rows: list[dict], creds: dict, http: _WhitelistedHTTP) -> None:
        self.rows = rows
        self.creds = creds
        self.http = http


def _run(spec: dict) -> dict:
    entry = spec.get("entry", "push")
    http = _WhitelistedHTTP(list(spec.get("allow_domains", [])))
    ctx = _PushContext(list(spec.get("rows", [])), dict(spec.get("creds", {})), http)
    try:
        ns: dict[str, Any] = {}
        exec(compile(spec["code"], "<push-component>", "exec"), ns, ns)  # noqa: S102 —— 管理员签名组件代码
        fn = ns.get(entry)
        if not callable(fn):
            return {"ok": False, "sent": 0, "error": f"component missing callable {entry!r}"}
        sent = fn(ctx)
        return {"ok": True, "sent": int(sent) if isinstance(sent, int) else len(ctx.rows), "error": None}
    except PushBlocked as exc:
        return {"ok": False, "sent": 0, "error": f"PushBlocked: {exc}"}
    except Exception as exc:  # noqa: BLE001 —— 组件任意异常都上报父进程决定退避/死信
        return {"ok": False, "sent": 0, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        http.close()


def main() -> None:
    spec = json.loads(sys.stdin.read() or "{}")
    result = _run(spec)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
