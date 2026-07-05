# pyp-agent

payipa 子节点采集端（导入名 `pyp_agent`）。出站 WebSocket 连主控（注册/心跳/领任务/回报/取消），三引擎抓取（niquests / curl_cffi / 自动化），大对象直传对象存储、回传结构化结果与指针。

**依赖纪律**：只依赖 `payipa-contracts` + jbutils + niquests/websockets/anyio；**禁止依赖 `payipa-core`**（import-linter CI 强制）。零 DB 凭证、零 S3 密钥。

一行接入：`pyp-agent join --server <URL> --token <一次性 join token>`。独立发 PyPI + Docker 镜像。
