# payipa-contracts

协议定义层：用 Pydantic v2 声明所有跨进程/跨模块传递的**数据形状**。零 I/O、零业务逻辑、零 DB、零密钥；依赖只有 pydantic。agent 与主控唯一的共同依赖。

模块：`agent` `task` `rule` `artifact` `result` `monitor` `event` `errors` `enums` `version`。

字段标注「已生效/未生效」（架构红线 00 §3.8）：description 前缀 `[已生效]/[未生效]` + `json_schema_extra={"x-effective": bool, "since_milestone": ...}`。破坏性变更须升 `version.CONTRACT_VERSION`。
