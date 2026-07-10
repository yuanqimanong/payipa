"""组装沙箱容器 runner（M3 slice-6）——**独立脚本，按路径执行**，不导入 payipa 包（容器内也没有）。

父进程（可信侧 SandboxExecutor）经 stdin 传一份 JSON：
``{source, entry, gateway_url, job_token, watermarks, page_limit, out_path}``。
本进程 exec 组装源码取固定方法 ``entry(ctx)``（与 LocalExecutor 的 AssembleFn 同约定），
``ctx.read_table`` 与 AssembleContext.read_table **同签名**——同一份脚本两种执行器都能跑；
取数走 Query Gateway HTTP（``X-Job-Token`` 头 + 签名不透明游标翻页；增量 = ``id>水位`` 过滤 +
客户端追踪最大 id），结果写 ``out_path``（默认 /out/result.json）——**stdout 只算日志**，
用户代码 print 不污染协议。

只依赖 stdlib；网络能到哪由容器网络收口（internal 网 + 路径白名单代理），本脚本不做也做不了扩权。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import urllib.error
import urllib.request

DEFAULT_OUT_PATH = "/out/result.json"


class GatewayError(RuntimeError):
    """Query Gateway 返回非 2xx（含越权 403 / 配额耗尽 403 / 令牌失效 401）。"""


def _filter_dict(f) -> dict:
    """归一化过滤条件：dict 原样、对象取 column/op/value（op 兼容 StrEnum 与裸字符串）。"""
    if isinstance(f, dict):
        return {"column": f["column"], "op": f["op"], "value": f["value"]}
    return {"column": f.column, "op": getattr(f.op, "value", f.op), "value": f.value}


class GatewayContext:
    """沙箱内交给组装脚本的唯一取数入口（与 AssembleContext.read_table 同签名）。"""

    def __init__(
        self,
        gateway_url: str,
        job_token: str,
        *,
        watermarks: dict[str, int] | None = None,
        page_limit: int = 500,
        http_timeout: float = 30.0,
    ) -> None:
        self._url = gateway_url
        self._token = job_token
        self._page_limit = page_limit
        self._http_timeout = http_timeout
        self._start_wm = dict(watermarks or {})
        self.new_watermarks: dict[str, int] = {}

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Job-Token": self._token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:  # noqa: S310 —— 网络面由容器收口
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise GatewayError(f"gateway HTTP {exc.code}: {detail}") from exc

    async def read_table(
        self,
        source: str,
        *,
        columns: list[str] | None = None,
        filters: list | None = None,
        limit: int | None = None,
        incremental: bool = False,
    ) -> list[dict]:
        """读某数据源全部（自动翻页）行。incremental=True 时从水位后读起并追踪最大 id（同 AssembleContext）。"""
        after = self._start_wm.get(source, 0) if incremental else 0
        fdicts = [_filter_dict(f) for f in (filters or [])]
        if incremental and after:
            fdicts.append({"column": "id", "op": "gt", "value": after})
        fetch_cols, strip_id = columns, False
        if incremental and columns is not None and "id" not in columns:
            fetch_cols = [*columns, "id"]  # 借 id 追踪水位
            strip_id = True
        rows: list[dict] = []
        cursor_token: str | None = None
        max_id = after
        while True:
            body: dict = {
                "source": source,
                "columns": fetch_cols,
                "filters": fdicts,
                "limit": limit or self._page_limit,
            }
            if cursor_token:
                body["cursor_token"] = cursor_token
            page = self._post(body)
            for row in page["rows"]:
                rid = row.get("id")
                if isinstance(rid, int) and rid > max_id:
                    max_id = rid
            rows.extend(page["rows"])
            cursor_token = page.get("next_cursor")
            if not cursor_token:
                break
        if incremental:
            self.new_watermarks[source] = max_id
            if strip_id:
                for row in rows:
                    row.pop("id", None)
        return rows


async def _run(spec: dict) -> tuple[list[dict], dict[str, int]]:
    ns: dict = {}
    exec(compile(spec["source"], "<assembly>", "exec"), ns)  # noqa: S102 —— 沙箱容器即执行边界
    entry = ns.get(spec.get("entry", "assemble"))
    if not callable(entry):
        raise RuntimeError(f"组装脚本缺少入口方法 {spec.get('entry', 'assemble')}(ctx)")
    ctx = GatewayContext(
        spec["gateway_url"],
        spec["job_token"],
        watermarks=spec.get("watermarks"),
        page_limit=int(spec.get("page_limit", 500)),
    )
    rows = entry(ctx)
    if inspect.isawaitable(rows):
        rows = await rows
    if not isinstance(rows, list):
        raise RuntimeError("assemble(ctx) 必须返回 list[dict]")
    return rows, ctx.new_watermarks


def main() -> None:
    spec = json.load(sys.stdin)
    try:
        rows, new_wm = asyncio.run(_run(spec))
        out: dict = {"ok": True, "rows": rows, "new_watermarks": new_wm}
    except Exception as exc:  # noqa: BLE001 —— 结果协议要求把任何失败编码回父进程
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    with open(spec.get("out_path", DEFAULT_OUT_PATH), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
