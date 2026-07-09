"""studio —— 组装（04）：Query Gateway、组装执行器、Loader、SchemaEvolver。

M3 slice-1 已落：`gateway.QueryGateway`（进程内结构化取数，红线2 唯一取数路径）、`executor`（CodeExecutor 协议
+ LocalExecutor + AssembleContext）、`asm`（asm_* 动态表 + 幂等 Loader）、`run.run_assembly`（data_*→Gateway→
组装→asm_* 主链）。后续切片：job_token JWT + 只读角色、HTTP+Arrow 网关、keyset/配额、版本+签名门、真
SandboxExecutor（专用出网 + sidecar 上传）、worker 池、增量水位、自动触发 + SchemaEvolver。
"""
