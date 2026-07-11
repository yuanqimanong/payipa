"""studio —— 组装（04）：Query Gateway、组装执行器、Loader、SchemaEvolver。

M3 已落：`gateway.QueryGateway`（进程内结构化取数，红线2 唯一取数路径）、`executor`（CodeExecutor 协议
+ LocalExecutor + AssembleContext，支持增量取数）、`asm`（asm_* 动态表 + 幂等 Loader）、`run.run_assembly`
（data_*→Gateway→组装→asm_* 主链，支持增量）、`store`（版本 + 签名门）、`cursor`（签名游标 + 配额）、
`watermark`（slice-8 增量读侧水位：读腿可重算 + 写腿幂等）、`evolve`（slice-9 SchemaEvolver：asm_ 加法演进
新增索引列 + 破坏性删除拦截；run_assembly 建表即经它）、`sandbox`（slice-6 真 SandboxExecutor：锁定 Linux
容器 + internal 网络 + 路径白名单 egress 代理 + job_token 数据面；Windows 走 Docker Desktop/WSL2；
`run.run_assembly_sandboxed` 编排）、`sandbox.SandboxPool`（slice-7 常驻 worker 池：预热 N 容器、
按空闲派发、/out 文件框定，摊薄冷启动；与 SandboxExecutor 同 run_source 签名，可直接传 run_assembly_sandboxed）。
"""
