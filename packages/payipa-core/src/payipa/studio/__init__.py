"""studio —— 组装（04）：Query Gateway、组装执行器、Loader、SchemaEvolver。

M3 已落：`gateway.QueryGateway`（进程内结构化取数，红线2 唯一取数路径）、`executor`（CodeExecutor 协议
+ LocalExecutor + AssembleContext，支持增量取数）、`asm`（asm_* 动态表 + 幂等 Loader）、`run.run_assembly`
（data_*→Gateway→组装→asm_* 主链，支持增量）、`store`（版本 + 签名门）、`cursor`（签名游标 + 配额）、
`watermark`（slice-8 增量读侧水位：读腿可重算 + 写腿幂等）。后续切片：真 SandboxExecutor（专用出网 +
sidecar 上传，Linux 依赖）、worker 池、自动触发 + SchemaEvolver。
"""
