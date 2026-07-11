"""常驻沙箱 worker（M3 slice-7 worker 池）——**独立脚本，按路径执行**，不导入 payipa 包。

与一次性 `_sandbox_child` 的区别：child 读一条 spec、产一次结果就退出（每作业一容器，冷启动）；
worker 常驻，从 stdin 逐行读 JSON spec，每条执行后把结果**原子写入 `spec['out_path']`**（先写
`.tmp` 再 rename），然后继续等下一条——容器保持温热，多作业摊薄容器创建开销。

结果走 /out 卷文件而非 stdout：用户脚本的 print 落 stdout（仅日志），不会污染框定协议。
每条作业在**全新命名空间**里 exec（复用 child._run）；同一 worker 进程跨作业不重建（模块级副作用会残留，
故 worker 池定位为受信管理员脚本的吞吐优化，完全逐作业进程隔离仍用一次性 SandboxExecutor）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from child import _run  # 同挂载在 /job 的一次性 runner，复用其 GatewayContext + 执行逻辑


def _write_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, default=str)
    os.replace(tmp, path)  # 原子出现，父进程轮询到即为完整结果


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            spec = json.loads(line)
            out_path = spec["out_path"]
        except ValueError, KeyError:
            continue  # 坏帧忽略（无 out_path 无法回报，父进程按超时处理）
        try:
            rows, new_wm = asyncio.run(_run(spec))
            result = {"ok": True, "rows": rows, "new_watermarks": new_wm}
        except Exception as exc:  # noqa: BLE001 —— 把任何失败编码回父进程，worker 不退出
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _write_atomic(out_path, result)


if __name__ == "__main__":
    main()
