# 01 · 数据获取与 Agent

状态：**已定案**（2026-07-03 开启，2026-07-04 收口；见 §4）

## 1. 需求对齐

需求方口述要点（2026-07-03，第 2 轮）：

- 爬虫调度**按任务分发**。
- 有一个独立的 **pyp-agent**：可通过 **pip / docker 快速部署**到其他节点机器；主节点与 N 个子节点互联。
- 互联方式开放讨论：主节点主动连接子节点？子节点主动出站连接主节点？或有更好的互联注册方案？
- pyp-agent 是否也放在 pyp 目录（仓库）下？
- agent 职责边界待定：是**自带下载、解析、入库全功能**，还是**回传数据到主控端再处理**？
  - 数据量大的时候怎么办？
  - 多媒体数据（图片/视频/文件）怎么办？
  - 或者 agent 自动拉取规则（配置脚本代码）再本地执行？
- 配置脚本代码可以人工写或 AI 帮写（AI 部分归 08 模块）。

待需求方拍板的问题清单：

1. 主↔子节点互联注册模式选哪种？
2. pyp-agent 的代码归属与发布形态（monorepo 内成员 / 独立仓；PyPI 包 + Docker 镜像）？
3. agent 职责边界：纯抓取回传 / 抓取+解析 / 全功能（含入库）？
4. 大数据量与多媒体的回传通道（是否引入对象存储、直传还是经主控中转）？
5. 规则/脚本如何下发到 agent（推 / 拉、版本化、校验）？

## 2. 技术调研

> 事实核验于 **2026-07-04**（WebSearch/WebFetch，标注一手来源）。

### 2.0 主↔子节点互联模式（已完成）

**核心结论：业界几乎一致采用「agent 主动出站发起连接（长轮询/流/连 broker），只需 outbound、无需入站」；全文统一使用“agent 主动出站连接”。**

- 逐个查证的连接模型（谁发起、协议、NAT 后可用性）：

| 系统 | 谁发起 | 协议 | NAT 后 | 认证 |
|---|---|---|---|---|
| GitHub Actions runner | runner 出站 | HTTPS 长轮询（~50s 窗口） | ✅ 只需 outbound 443 | 短期注册 token（1h 过期）|
| GitLab Runner | runner 出站 | HTTPS 轮询 `POST /jobs/request` | ✅ | 注册 token |
| Temporal worker | worker 出站 | gRPC 长轮询（阻塞 ~60s），连 7233 | ✅ | mTLS / API key |
| Prefect worker | worker 出站 | HTTP 轮询 work pool（默认 15s） | ✅ | API key |
| Celery worker | worker 出站连 broker | 连 Redis/RabbitMQ 拉任务 | ✅ | broker 凭据 |
| Crawlab 节点 | worker 出站连 master | **gRPC 双向流**，~5s 心跳，>60s 判离线 | ✅ | 节点 auth key |
| scrapyd | **中控入站连节点** | 每节点跑 HTTP JSON API（默认 6800） | ❌ 节点不 phone-home | 默认无鉴权 |

  除 scrapyd 外全是「agent 出站 + 拉模型」。scrapyd 的中心入站模型**不适合 NAT 后节点**。

- **术语统一**：正式文档使用「agent 主动出站连接 / outbound worker / pull 模型」。该表述准确说明连接由 agent 发起，主控不需要访问节点地址，也与 GitHub、GitLab、Temporal 的公开文档一致。

- **三种连接技术在 2026 的现状与取舍**：

| 维度 | HTTP 长轮询 | WebSocket | gRPC 双向流 | 消息代理(NATS/Redis) |
|---|---|---|---|---|
| 连接方向 | agent 出站 | agent 出站 | agent 出站 | agent 出站连 broker |
| NAT 穿透 | 优 | 优 | 优 | 优 |
| 实时性 | 中（秒级） | 高 | 高（亚秒） | 高 |
| 实现复杂度 | **低**（FastAPI 路由 + httpx/niquests） | 中（自管重连/心跳/代理超时） | 高（proto/编译/流生命周期） | 中（多运维一个 broker） |
| 依赖 | 轻 | 中（websockets 16.0，需 Py≥3.10） | 重（grpcio 1.81.1） | 重（需高可用 broker） |
| 认证 | Bearer/mTLS | token/mTLS | mTLS/per-RPC token | broker 原生 |

- **grpcio 对 3.14 的硬门槛已过**：grpcio 1.81.1（2026-06-11）在 PyPI 有齐全 cp314 标准 wheel（Win/Linux/mac），pip 直装无需编译；唯一缺口是 free-threaded（cp314t）wheel 仍缺（grpc#41461）——我们生产用普通 GIL 构建，无影响。
- **httpx 再次确认停滞**（PyPI 最新仍是 0.28.1 / 2024-12），agent HTTP 客户端建议 niquests 3.20.0。
- **broker 路线（NATS/Redis/MQTT）**：几台到几十台节点规模属「可行但偏重」——多一个必须高可用的中间件（broker 挂=全网停摆）。若非已用 Redis，单为分发任务引入 broker 边际收益有限；真要用，NATS 最轻（原生 pub/sub + queue group 负载均衡 + JetStream 持久化），MQTT 无任务队列语义需自造、除非弱网/嵌入式否则不如 NATS。

**⚠️ 过程结论（已被取代，保留供追溯）**：本节调研当时倾向「agent 出站 HTTP 长轮询 + call-home 心跳」。**最终定案 = agent 出站 WebSocket 持久连接**（见 §2.5① 与 §4-1）——因需要主控向 agent 强实时下发（即时取消等），WS 单通道承载注册/心跳/任务/状态更合适；认证仍用注册 token + 任务级短期 token（对齐 GitHub/GitLab）。

### 2.3 浏览器插件点选回填（已完成）

调研结论详见 [10-浏览器插件.md](10-浏览器插件.md) §2：MV3 强制、Firefox 不支持 externally_connectable、推荐「平台 UI 页 ↔ 扩展浏览器内直通」架构（零服务器新增面、无配对流程）。

### 2.1 大对象 / 多媒体回传（已完成）

**行业共识：控制面/数据面分离——小结构化结果走 API/队列回控制面，大 blob 走对象存储直传，控制面只收「指针」（key + ETag + size + hash）。**

- **「主控只发凭证、数据不经手」是标杆模式**：GitHub Actions Artifacts v4 即此实现——后端签发 scoped 到特定路径的临时凭证（SAS/presigned URL），runner 分块直传对象存储，取消中间代理后上传最高提速约 90%（github.blog，2026-07-04 核验）。反例是 GitLab Runner 让数据全量过服务端，GitLab 自己也在服务端内部尽量不经过应用进程——说明「数据过主控」是历史包袱不是优选。
- **S3 presigned URL / multipart 硬限制**（AWS 官方 qfacts）：单 PUT ≤5 GiB；multipart 每块 5 MiB–5 GiB、最多 10,000 块、单对象上限约 5 TiB；presigned URL 用 IAM 长期凭证签最长 7 天，**用临时凭证签则随凭证过期提前失效**（长任务需支持按需补签）。>100 MB 即建议走 multipart。断点续传用 multipart 原生机制（持久化 UploadId → ListParts 查已传 → 补传 → Complete），**不需要引入 tus 协议**；必须配 `AbortIncompleteMultipartUpload` lifecycle 清理孤儿分块。
- **自托管对象存储选型（重要变化）**：**MinIO 已出局**——GitHub 仓库 2026-04-25 正式归档（read-only），README 明示不再维护，社区版控制台/二进制自 2025 年逐步移除，导流到商业版 AIStor。替代品现状：**SeaweedFS**（Apache-2.0，4.37 版 2026-06-29，约每周一版，Haystack 架构对海量小文件 O(1) 读，最契合采集场景，社区判断为原 MinIO 场景默认替代）；Garage（AGPLv3，小规模/异地分布、单二进制 1GB RAM 可跑）；Ceph RGW（S3 兼容面最广但运维最重）；RustFS（1.0 beta，观望）。三者都支持 presigned URL + multipart 直传。
- **Python 客户端分工**：主控端用 boto3 签 URL、管 multipart 生命周期；agent 端上传 presigned URL 只需 HTTP 客户端 PUT（无需 AWS SDK），或用 obstore（Rust 绑定、原生 async，未到 1.0）。

**对「agent 职责边界」和「大数据/多媒体」问题的启示**：agent 抓取产出的大对象（HTML 批量、图片/视频）直传对象存储、只回传指针给主控；结构化 item/状态走任务结果 API。这天然解耦了「agent 是否入库」——入库动作留在主控侧对指针和结构化结果做，agent 不持有数据库凭证（避免 Crawlab「worker 直连 DB」、scrapy-redis「共享 Redis」那种把 DB 凭证摊到所有节点的反模式，节点在不可信网络时尤其危险）。

### 2.2 规则 / 脚本下发到 agent（已完成，随回传一并调研）

- **分发模式**：内容寻址包（按 hash 命名、immutable、可无限缓存）+ 通道指针文件（stable/canary，含 {version, sha256, url}）+ ETag 条件 GET 轮询；或复用任务长轮询通道捎带版本号。灰度靠切 canary 指针。参照 GitLab Runner「领任务即原子下发完整 job payload + 任务级短期 token」。
- **信任边界（关键）**：**分发通道 ≠ 信任来源**。agent 不能因为「包从主控/TLS 正常拉来」就执行命令式代码，应校验独立发布签名（ed25519/minisign/cosign），**签名私钥离线保管、不放主控端**。即使主控状态异常，agent 也只执行经过独立签名和版本固定的内容。体系化可参考 TUF（角色分层、阈值签名、防回滚），最小可行方案 = 离线密钥签名 + agent 验签 + 版本 pin + 沙箱执行 + hash 审计。声明式规则（选择器/JSON，agent 内置解释器）风险远低于命令式脚本，可简化分发流程。
- **反向边界**：每 agent 独立身份与最小 scope 凭证（presigned URL/STS policy 只允许写 `results/{agent_id}/` 前缀），可单独吊销。

### 2.4 第 3 轮问题的设计澄清（2026-07-04）

**① 出站长轮询是否需要「双向配置」？——不需要，这正是它的最大优点。**

- 主控**完全不需要知道子节点的地址**（内网 IP、端口都不用配）；只有子节点需要两样东西：主控 URL + 注册 token。配置是**单向的、一行命令**：
  ```
  pip install pyp-agent
  pyp-agent join --server https://pyp.example.com --token <一次性注册token>
  ```
- 注册 token 流程对齐 GitHub Runner：UI 上点「添加节点」→ 生成短时效一次性 token（含完整 join 命令，复制即用）→ 节点上执行 → agent 自报 hostname/能力完成注册、换取长期节点凭证 → UI 里出现该节点。**没有任何主控侧人工配置**。
- 之后的心跳、领任务、回结果都是 agent 出站发起，主控只是被动应答。

**② WebSocket 和 gRPC 不是一回事；大数据量不走控制通道，所以都没有瓶颈问题。**

- 区别：WebSocket = 在 HTTP 上升级出来的**裸双向消息管道**（内容格式自定）；gRPC = 基于 HTTP/2 的**RPC 框架**（proto 强类型契约 + 代码生成 + 流式调用）。两者都成熟稳定（WS 是 RFC 6455 标准；gRPC 是 Google/CNCF 出品，Temporal/Crawlab 都在用）。
- 关键设计原则：**控制通道只传「小消息」（任务指令、心跳、状态、指针），大数据走数据通道（对象存储直传/HTTP 上传）**。gRPC 默认单消息上限 4MB，WS 传 GB 级也不现实——但这不是缺陷，因为本来就不该用控制通道搬数据。业界（GitHub/GitLab/Temporal）全部如此分层。
- 结论：选型只关乎控制通道的实时性需求。**本项目因需即时取消等主控主动下发，v1 直接定 WebSocket**（见 §2.5①/§4）；gRPC 不需要。（本段为 §2.4 澄清期分析，"长轮询作 v1 载体"的说法未被采纳。）

**③ 大对象直传 ≠ 给子节点发密钥——密钥永远不出主控。**

- presigned URL 的机制：主控持有唯一的 S3 密钥，用它对「某一个对象 key + 某个操作 + 有效期」做本地 HMAC 签名，产出一个**短时效、单对象、单操作**的 URL 给 agent；agent 拿这个 URL 直接 PUT，**全程不持有任何密钥**。URL 过期即废、泄露也只影响那一个对象的那次操作。
- 「频繁请求」的担心不成立：签名是主控本地的纯计算（微秒级，不与 S3 交互）；且 URL 可以**随任务/回传握手顺带批量签发**（如 multipart 一次签一批分块 URL），不产生独立的高频取密钥流量。
- 因此**不需要单独做一个大文件中转服务**——那反而把数据流量拉回主控（GitLab 的历史包袱模式）。唯一例外：用户没有对象存储时，主控提供一个流式上传接口兜底（见④），此时数据过主控是明知且可接受的降级。

**④ 用户没配置对象存储时，大文件存哪？——主控端主机，绝不落子节点。**

- 存储抽象两个后端：`s3`（推荐，配置了就直传）/ `local`（兜底，主控数据目录，agent 经主控的流式上传接口回传）。
- 不落子节点本地的原因：数据必须集中可查、可管理、可备份；子节点随时可能下线/重装，且分散存储会让「数据在哪」不可回答。
- UI 行为：未配置对象存储时正常可用（local 后端），但在存储设置页与大文件任务处**提示「建议配置 S3 兼容存储以启用节点直传」**；数据量/节点数上来后 local 后端的吞吐瓶颈（数据全过主控）就是切 S3 的信号。

**⑤ 规则是「下发」还是「去规则库获取」？——混合：任务带指针，agent 按需拉取 + 内容寻址缓存。**

- 任务消息里**不带完整规则**，带 `(rule_id, version, content_hash)` 三元组（几十字节）。
- agent 收到任务 → 查本地缓存有无该 hash → 有则直接用（零网络）；无则向主控规则接口按 hash 拉取一次，验 hash 后落缓存。
- 好处：任务消息恒小；规则按内容寻址天然免疫篡改与「拉到旧版」；同一规则跑一千次任务只拉一次；任务里 pin 了版本，重跑可复现；规则更新 = 新任务带新 hash，天然灰度。
- 所以流程是：你发任务（含规则指针）→ agent（缓存或拉取规则）→ 执行 → 大对象直传存储 → 回传结构化结果 + 指针。

### 2.5 第 4 轮设计细化（2026-07-04）

**① WebSocket 已定案，落地要点**：

- 连接方向不变：仍是 agent 出站发起（join token 一行命令接入流程原样保留），只是把「反复长轮询」换成「一条常驻 WS」，注册/心跳/领任务/状态上报共用这一条通道。
- 必做三件事（调研已证实是 WS 的隐性成本）：应用层心跳 15–30s（空闲 WS 会被 LB/NAT 在 60–300s 静默断开）；断线**指数退避重连**（断网/主控重启后 agent 自愈）；反代放开长连超时（如 Nginx `proxy_read_timeout`）。MV3 无关，服务端 FastAPI 原生支持 WS，agent 端用 websockets 16.x（需 Py≥3.10，我们是 3.14，无碍）。
- 纪律重申：**WS 只传控制消息**（指令/心跳/状态/指针，KB 级）；大对象永远走数据通道。

**② 工件（artifact）元数据：DB 记录的是永久对象 key，不是会失效的 URL**：

- presigned URL 是**一次性传输凭证**，用完即弃、不落库。落库的是永久引用与排查所需的全部关联：

| artifacts 表字段（要点） | 说明 |
|---|---|
| `bucket` + `object_key` | 永久地址，如 `results/{task_id}/{attempt}/{filename}`——key 命名即自带关联 |
| `size` / `sha256` / `etag` / `content_type` | 完整性校验与去重依据（上传完成时由 agent 回报、主控 HeadObject 复核） |
| `task_id` / `attempt_id` / `agent_id` / `source_id` | 排查回溯的关联键 |
| `storage_backend`（s3/local）/ `status` / `created_at` | 后端标识、生命周期状态 |
| `owner_id` / `tenant_id`（预留）/ `expires_at` | 资源归属与保留期（GC/权限/审计，2026-07-05 SDD 回写） |

- 事后访问（UI 下载、重解析、排查）= 按 object_key **现签一个新的 presigned GET**，秒级、随用随签。
- 日志关联：任务全链路（下发→执行→上传→落库）共用 `task_id`/`attempt_id` 作 correlation id，日志、artifacts、任务事件三者能互相跳转。

**③ local 兜底的回传通道：HTTP 流式分块上传，不走 WS**：

- WS 是控制通道，搬大文件既低效又会把心跳堵死。local 后端下主控暴露一个**流式分块上传接口**（分块 PUT + 断点续传语义，对齐 S3 multipart 的分块尺寸约定），agent 侧代码与直传 S3 是**同一套**（storage 抽象返回「上传目标」：s3 后端给 presigned URL 组，local 后端给主控上传端点 + 一次性上传 token）——agent 不感知后端差异。
- **磁盘治理（你提的预留空间检查，必做）**：主控启动与运行期检查数据目录**磁盘水位**（低于阈值：拒绝新上传任务并告警，不影响已在途的）；可配 per-task/per-source 容量上限；配保留期策略 + 定期清理（artifact GC）；UI 存储页展示用量与水位告警。

**④ 规则缓存更新与测试通道**：

- **缓存不需要「更新机制」**——这是内容寻址的设计红利：缓存条目按 hash 不可变；规则一改就是新 version + 新 hash，新任务引用新指针，agent 未命中自动拉取。旧条目按 LRU/保留期清理即可，永远不存在「缓存里是旧规则」的问题（任务 pin 了 hash，要旧版本反而是特性：重跑可复现）。
- **测试通道（不污染正式环境）**三层设计：
  1. **规则版本状态机**：`draft → 试跑(testing) → 发布(active)`；draft/testing 版本只能被测试任务引用；
  2. **任务通道**：任务带 `channel=test|prod` 标记；test 任务的产出落**隔离的测试数据集**（staging 区，不并入正式 canonical 数据、不触发正式 Deliver），验证通过后规则 publish、正式任务才产出正式数据；
  3. **节点分组**：可指定「测试节点组」执行 test 任务（或任意可用节点执行但打 test 标记），避免占用/干扰正式采集；agent 侧无需感知 test/prod——它只按任务里的存储指针写结果，隔离由主控在下发时决定存储目标与标记。
- 细节（状态机字段、通道模型、节点分组）在后续「规则/清洗」与「任务调度」模块文档展开。

## 3. 可行性验证

- ✅ grpcio 1.81.1 有齐全 cp314 wheel（未来若需 gRPC 无版本障碍，2026-07-04 核验）；WS 用 websockets 16.x，Py 3.14 满足。
- ⏳ 待办 POC（开工后）：presigned URL 直传对自有 S3 的 multipart 全流程兼容冒烟；local 后端流式分块上传 + 断点续传；WS 断线重连 + 反代长连超时的稳定性演练。

## 4. 定案（2026-07-04）

1. **互联 = agent 出站 WebSocket 持久连接**；一次性 join token 一行命令接入；WS 只传小消息，大数据走数据通道。
2. **agent 职责 = 抓取 + 大对象直传存储 + 回传指针与结构化结果**；入库在主控侧；agent 零 DB 凭证、零 S3 密钥。
3. **大对象直传 = presigned URL**（密钥不出主控）；**DB 永久记录对象 key** 及 task/attempt/agent/size/sha256 关联，presigned URL 不入库、随用随签。
4. **无对象存储兜底 = 主控端本地盘**（agent 经主控 HTTP 流式分块上传，不走 WS）；主控做磁盘水位检查与告警；UI 提示建议配 S3。
5. **规则获取 = 任务带 (rule_id, version, hash) 指针 + agent 内容寻址缓存 + 未命中拉取**；缓存不可变无需失效通知；配版本状态机 + test/prod 测试通道 + 节点分组防污染。
6. **pyp-agent 为 monorepo workspace 成员**，独立发 PyPI + Docker（见 [00 §3.5](00-总纲与全局约束.md)）。
7. **agent 自动化能力（可选，2026-07-05；2026-07-10 收紧边界）**：动态页渲染与交互采集是 **agent 的一项可选能力**，非全 agent 必备；统一使用标准 **Playwright**，仅执行正常页面交互和已授权登录流程。agent **注册时上报能力**（是否支持自动化）；需要自动化的任务经 **agent 分组** 分发到有该能力的集群（[07 §4-16](07-任务与调度.md)）。浏览器上下文可绑定经批准的中转出口以保持会话路由稳定，但不得用于改变访问身份或绕开站点访问控制。
8. **HTTP 下载引擎（2026-07-10 修订）**：常规 HTTP 数据源统一使用 **niquests**（HTTP/2/3、连接复用、requests 兼容）。动态渲染或正常交互改用标准 Playwright；如果数据提供方要求专用客户端、认证方式或协议参数，只按其公开文档和授权配置接入，无法兼容时暂停并人工处理，不模拟其他客户端身份。

## 5. 遗留问题

- 命令式脚本签名：已定 **管理员统一签名、用户无感**（[02 §定案-2](02-数据清洗.md)）；离线密钥保管的运维细节留实现期。
- agent↔主控的 WS 消息 schema（注册/心跳/任务/状态/取消帧）——在 `payipa-contracts` 中定义（见 [00 §3.5](00-总纲与全局约束.md)）。
- 节点分组、per-source 容量配额、artifact 保留期策略的具体默认值——待任务调度/部署模块。
- Playwright 浏览器版本、镜像体积和 Win/Linux 冒烟矩阵——有真实自动化需求时定（[11](11-代理池.md) 出口绑定已通）。
