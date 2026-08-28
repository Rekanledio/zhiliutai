# 知流台当前最终架构

> 本文只描述当前真实架构，不保留 Lite/Full、PostgreSQL 版或第三方平台版等平行方案。稳定产品范围以 `docs/PROJECT.md` 为准。

## 1. 运行拓扑

~~~text
React + TypeScript + Vite
          │ REST / JSON + SSE
          ▼
FastAPI
 ├─ Sources / Items / Review / Jobs / Obsidian
 ├─ Python JobRunner
 ├─ Obsidian polling watcher + rescan
 ├─ OpenAI-compatible capability adapters
 ├─ RAG services + QuestionAnswerGraph
 ├─ IngestionGraph（HITL/checkpoint 编排）
 ├─ MCP Server（五工具）+ 受控 MCP Client（显式 profile）
 ├─ Backup / Restore / Derived Rebuild service
 │
 ├─ SQLite + FTS5
 ├─ 独立 checkpoint SQLite（LangGraph 自管 schema）
 ├─ Qdrant Python Client local persistence
 ├─ data/artifacts
 └─ Obsidian Vault
~~~

本机开发只启动 FastAPI 和 Vite；SQLite、Qdrant Local、JobRunner 均随 Python 应用工作，不需要 Docker Desktop 或独立服务。所有本机服务默认绑定 `127.0.0.1`。

## 2. 数据所有权

| 数据 | 唯一主来源 | 当前实现 |
| --- | --- | --- |
| 用户确认后的知识正文 | Obsidian Markdown | 网页编辑与 watcher 都读写同一文件 |
| 原始输入与处理产物 | `data/artifacts` | SHA-256 内容寻址，不由用户文件名决定内部路径 |
| 业务元数据、版本、任务、Chunk、ModelRun、Citation | SQLite | `data/zhiliutai.db`，SQLAlchemy + Alembic |
| 全文索引 | SQLite FTS5 | `chunk_fts`，从 Chunk/Markdown 可重建 |
| 向量索引 | Qdrant Local | `data/qdrant/`，不保存业务主状态 |

SQLite 默认启用 foreign keys、WAL 和 busy timeout。测试使用临时 SQLite 并执行真实 Alembic migration。Qdrant payload 包含 `chunk_id`、`knowledge_item_id`、`content_version_id`、`source_type`、`source_locator`、`embedding_model` 和 `embedding_version`。

Chat 与 Embedding 是独立 capability。默认 Chat adapter 使用 OpenAI-compatible API；默认 Embedding adapter 使用进程内 FastEmbed 的中文 `BAAI/bge-small-zh-v1.5`（512 维），模型缓存位于 `data/models/fastembed/`。远程 OpenAI-compatible Embedding 仍是受支持的显式配置选项。

## 3. 当前领域模型

阶段 2 已建立并实际使用：

- `KnowledgeItem`：逻辑条目、状态、软删除和当前版本指针；0004 增加可为空的
  `pending_content_version_id`，两者不能指向同一版本，且 pending 版本必须属于当前
  KnowledgeItem。ORM 的本表约束由 SQLite migration 中的命名 trigger 补足跨表所有权校验。
- `SourceArtifact`：不可变原始输入及内容哈希；0004 增加结构化
  `metadata_json`、`retention_policy`、`retention_expires_at`、`cleanup_state` 和
  `cleaned_at`，并以 `ix_source_artifacts_cleanup_due` 支持到期清理查询；
  `until_expiry` 必须有非 NULL 的 `retention_expires_at`。
- `ContentVersion`：草稿/Vault 派生版本、摘要、标签建议和 Prompt 版本。
- `NoteBinding`：`zhiliu_id`、Vault 相对路径、内容哈希与同步状态。
- `Chunk`：SQLite 中的可追踪文本与引用定位；向量写入 Qdrant。
- `ProcessingJob` / `JobAttempt`：持久状态、进度、心跳、重试、结构化错误和失败历史。
- `ModelRun`：记录 RAG provider、Prompt、参数、输入/输出快照、Token、耗时和安全错误码。
- `Citation`：记录一次回答使用的 Chunk、内容哈希、ContentVersion、locator、target 和检索分数快照。

`Tag` 仍会在对应后续业务阶段落地；`Collection` 与 `CollectionItem` 已在阶段 6 批次 D
通过 0006 最小关系模型落地，供受控 MCP 查询使用。`Citation` 已在阶段 4 作为问答审计快照
落地，不形成用户可编辑正文主库。

## 4. JobRunner

API 先创建 `ProcessingJob`，JobRunner 再领取 `queued` 任务并写入 `JobAttempt`。状态为 `queued/running/succeeded/failed/cancelled`。失败可安全重试；启动时将中断的 running attempt 记为失败并重新排队，保留历史。FastAPI `BackgroundTasks` 不是任务状态主来源。

## 5. 阶段 2 闭环

~~~text
Text / Markdown
→ content normalization + SHA-256
→ content-addressed SourceArtifact
→ durable ingestion job
→ AI draft（未配置 Chat 时明确 passthrough）
→ pending review
→ explicit review
→ atomic Markdown write with stable Frontmatter
→ NoteBinding + ContentVersion
→ chunk + FTS5 + Embedding + Qdrant
→ published
→ watcher/rescan detects external edit or rename
→ new ContentVersion + current-only derived index
→ switch current version only after index succeeds
~~~

写 Vault 后若数据库阶段失败，受管理 Markdown 仍带稳定 `zhiliu_id`，rescan 可恢复绑定与派生状态。索引失败不会切换当前版本。删除知识条目只软删除业务记录和派生向量，不删除 Vault 文件。

当前 watcher 在单 API 进程内轮询受管理 Markdown，并提供显式 rescan。它通过最小文件年龄、读前/读后 stat 和共享 mutation lock 避免处理仍在写入的文件；瞬态不完整 Markdown 标记 error 并继续提供最近一次有效版本，不误判 missing。它忽略临时文件和无 `zhiliu_id` 的外部 Markdown；重复 ID 标记 conflict，文件缺失标记 missing，重命名只更新绑定路径。

`ContentVersion` 保留每次稳定修改的历史；Chunk、FTS5 与 Qdrant 是可重建的当前检索投影。每次发布、网页修改、watcher 重索引和进程首次 rescan 都会按 `knowledge_item_id` 收敛 Qdrant，并确保 SQLite Chunk/FTS5 与 `current_content_version_id` 对齐。未来若改为多进程，必须先引入跨进程协调，不能直接复制 watcher。

## 6. 阶段 3 统一 Source Pipeline

阶段 3 的 PDF、DOCX 和静态网页入口复用同一条来源管线：输入校验 → 原始
SourceArtifact → 持久化 ProcessingJob → 来源获取/解析 → 草稿 → 审核 →
Vault 发布 → Chunk/FTS5/Qdrant 当前版本索引。文本/Markdown 继续使用同一个
Job handler，不另建第二套业务流程。

- POST /api/sources/files 只接受 PDF 和 DOCX，上传内容进入受控的内容寻址
  Artifact；文件名只用于显示和来源元数据，不决定存储路径。
- POST /api/sources/url 只接受无需登录的静态 HTML。请求不携带 Cookie；URL
  只允许 http/https、不允许用户凭据，DNS 解析出的每个地址都必须是公网地址。
  每一跳重定向都会重新执行校验，并受超时、类型、大小和跳数限制。
- 解析器输出 SourceBlock。PDF block 保存 page/page_label，DOCX block 保存
  heading_path/heading_level 或表格行，网页 block 保存最终 URL 和标题层级。
  解析出的 block 文本及 locator 以可重建的 ContentVersion.source_metadata_json
  保存，不形成用户可编辑正文主库。
- IndexService 从这些 segments 生成带 JSON locator 的 Chunk.source_locator；
  Qdrant payload 继续复用相同 locator，因此 CitationBuilder 可以回到 PDF 页码、
  DOCX 标题层级或网页 URL；CitationBuilder 再从 SQLite 元数据生成安全的 exact/fallback/unavailable
  locator 和 target。网页原始 URL 请求和最终 HTML 快照都作为
  SourceArtifact 保留。

合成 PDF/DOCX fixture 由 backend/tests/fixture_sources.py 在测试中确定性生成，
不提交来源不明的二进制材料。

## 7. 阶段 4 RAG 检索与问答

HybridRetriever 先从 SQLite 快照取得 published、未软删除且具有
current_content_version_id 的条目；全文通道查询 SQLite FTS5，向量通道只使用
Qdrant Local 的 current-version payload filter。两路候选使用加权 RRF 去重，再回到
SQLite 复核并重建 Chunk 内容、标题、版本和 locator；Qdrant payload 的条目、版本、
source locator 和 embedding 标识与 SQLite 当前 Chunk 不一致时丢弃。Qdrant payload
不是版本权威来源。FTS、向量和 RRF 的并列结果使用当前 SQLite Chunk 的内容哈希、
版本哈希、ordinal、locator 和正文作为稳定排序键，不使用随机 UUID 决定排序。

EvidencePolicy 只对最终返回的 top-k Chunk 评估证据状态。没有证据或置信度不足时，
QuestionAnswerService 直接返回拒答，不调用答案 Provider。答案 Provider 只接收带
citation ID 的证据材料；结构化 claim 必须绑定本次 CitationBuilder 生成的合法 ID。
模型调用前建立当前版本快照；模型返回后，在共享 mutation lock 下由同一 SQLite
事务复核当前 Chunk/ContentVersion/KnowledgeItem，并与 ModelRun 成功状态和 Citation
写入一起提交。版本变化时本次运行失败并安全重试或返回冲突。

POST /api/search 返回搜索结果和结构化 citation；CitationBuilder 使用当前
ContentVersion.source_metadata_json、已验证 SourceArtifact 和 synced NoteBinding
生成 exact/fallback/unavailable locator，不从 Chunk.source_locator 猜测页码、标题、
URL 或路径。POST /api/chat/stream 在完整问答结果准备好后按 meta、delta、citations、
done 顺序发送事件，前端也拒绝乱序流。ModelRun/Citation 是脱敏审计快照，不是用户
确认正文主库。阶段 4 不引入完整 LangGraph/QuestionAnswerGraph。

RerankerProvider 只是可注入的普通 Protocol；未配置或失败时保留 RRF 并返回降级诊断。
当前只提供本地确定性 keyword-overlap 参考实现和固定中文离线评测，不伪造生产 HTTP
协议。

## 8. Provider 与 Graph 边界

Chat、Embedding、ASR、Vision、Reranker 是独立 capability。阶段 2 使用 Chat 与 Embedding；测试注入确定性 provider，不读取真实 secret。

阶段 5 的视频处理仍由普通 application service 和 JobRunner 编排，不引入新的 Graph。
`POST /api/sources/video` 只接收 `VideoSourceRequest` 的 URL、标题、语言、幂等键和视觉
开关；`VideoService` 复用 `ProcessingJob`、失败/重试/取消和 0004 的 pending/current
边界。视频入口、来源 URL Artifact 和处理结果均不把 SQLite 或 Artifact 当作用户确认正文
主库，确认后的正文仍只由 Obsidian Markdown 承载。

视频采集的安全链路如下：

1. `SourceFetcher.validate` 是 URL 安全策略的单一入口，拒绝非 HTTP(S)、凭据、敏感查询
   参数、本地路径和受保护 DNS 地址；它不让 HTTP 客户端隐式跟随重定向，而是对每个显式
   目标重新进行 scheme、DNS、端口、大小、响应类型和超时校验。这个保证不被外部 downloader
   自动继承或夸大。
2. `VideoSourceProvider`/`VideoDownloader` 只接受已验证 URL、受控 `VideoDownloadOptions`
   和 Artifact 根内的临时目录。`YtDlpDownloader` 没有直接联网 fallback；默认使用每次调用
   独占的 `LoopbackYtDlpNetworkExecutor`，也允许注入等价执行器。它只在 `127.0.0.1` 临时
   端口监听，对每个 HTTP 目标和 HTTPS `CONNECT` 目标解析一次并检查全部地址，任何受保护
   地址都会使该连接失败；实际 socket 直接连接同次校验得到的 numeric IP，从而关闭校验与
   连接之间的 DNS-rebinding 窗口。代理同时限制连接数、请求数、响应字节数和超时。
   适配器只传入固定参数、显式 loopback 代理、`--downloader native`、
   `--ignore-config`/`--no-plugin-dirs`、无 shell 的 `create_subprocess_exec` 和隔离环境，禁止
   把网络下载委托给不在该边界内的 ffmpeg/curl/aria2c。HTTPS payload 保持端到端加密，因此
   代理不伪造明文 redirect audit trail，也不能精确报告同一隧道内的重定向次数；安全保证是
   每个实际连接目标均重新校验，并由整体请求/时间上限收口。注入执行器如能返回可信链，仍按
   `max_redirects`、首尾 URL 和每一跳复核。
3. 有字幕时先使用严格 VTT/SRT/JSON3 parser 规范化为 `TranscriptSegment`；没有字幕只进入
   `asr_required`，不在该分支隐式调用 ASR。需要兜底时，`FfmpegAudioExtractor` 通过注入
   runner、固定无 shell 参数、受控目录、超时和大小限额生成 WAV，再交给明确的
   `ASRProvider` Protocol。
4. 仅显式开启视觉且来源类型为 `slideshow`/`tutorial` 时运行 `SceneDetector`、去重后的
   关键帧、`VisionProvider` 和可选 `OCRProvider`；访谈/播客跳过。转录、章节、关键帧和
   视觉事件共用非负整数毫秒时间线，manifest 在持久化前做时长、顺序、文本和引用校验。
   Provider/model 只记录标识和版本，不承载密钥。

处理结果按 `subtitle_ready`、`asr_complete` 或 `asr_required` 标记。来源 URL、媒体、字幕、
转录、关键帧和 manifest 都使用内容哈希、生成的内部相对路径和 0004 保留字段；网络媒体
默认处理后删除，`until_expiry` 由清理任务按到期时间删除，字幕/来源元数据/manifest/关键帧
按永久策略保存。任务失败或取消只清理未持久化临时产物，不切换旧 current/published。

视频采集先创建 pending `ContentVersion`。审核发布时，候选 Markdown 先写入 Vault 同目录
暂存文件；Embedding、候选 Qdrant 向量、SQLite Chunk/FTS5 更新在候选版本事务中准备，
只有这些步骤成功才交换正式 Markdown 并切换 current。交换后校验或事务提交失败会恢复旧
字节快照、回滚 SQLite 并按候选版本删除向量；提交后旧向量清理失败只产生可重试的孤儿，
SQLite current 复核仍禁止其成为证据。`CitationBuilder` 对视频 transcript、chapter、
keyframe locator 同时核对 current SQLite version、来源 Artifact、manifest、内容哈希、
时间段和关键帧 Artifact，全部满足才返回 exact，否则 fallback/unavailable。前端视频引用
通过受控 `/api/artifacts/{id}/locator` 在应用内展示已授权字幕片段或关键帧，不把无效的
媒体 `#t=` fragment 当作跳转保证，也不依赖已清理的原始网络媒体。

LangChain 只用于文档、切分、Embedding、Retriever、Prompt/Message、LLM 和 Tool 等合适抽象。最终两个主要 LangGraph 是 `IngestionGraph` 与 `QuestionAnswerGraph`；Graph 负责编排、路由、条件分支和需要的 checkpoint/HITL，业务逻辑留在普通 service。

## 8.1 阶段 6 批次 A：Graph 安全契约与骨架

批次 A 已建立独立的 `backend/app/workflows/` 编排层，但尚未接入生产 API、
JobRunner、Stage2Service、VideoService 或现有 RAG Provider。

- `contracts.py` 使用 extra=forbid 的 Pydantic 边界模型和 TypedDict/Literal state；
  state、interrupt payload 和服务结果只允许 UUID、Literal、稳定 error code、短文本和
  citation ID 等 JSON 安全原语。确认正文、Chunk、Embedding、Provider 输出和连接对象
  不进入 state。
- `runtime.py` 在第一次 Graph 调用前完成 query 脱敏、NFKC/长度校验与 resume 决策
  校验；thread_id 由 runtime 生成或接受已验证 UUID，不进入 state。编译图入口再次拒绝
  raw Command/update，防止绕过边界。
- `checkpoints.py` 使用独立的 AsyncSqliteSaver 文件（默认相对路径
  `data/checkpoints/workflows.db`），显式执行 setup、WAL/busy timeout 和关闭；其
  checkpoints/writes schema 由依赖自管，不进入业务 SQLite/Alembic。
- IngestionGraph 只编排注入的 process/review/publish Protocol，并在 review/publish
  interrupt 前不执行副作用；QuestionAnswerGraph 只编排注入的 retrieve/answer Protocol，
  evidence_status 非 sufficient 时拒答且不调用 answer。

## 8.2 阶段 6 批次 B：生产 IngestionGraph 编排

批次 B 已将现有文本、Markdown、PDF、DOCX、静态网页和视频 JobRunner handler 接入
`IngestionGraph`。`Stage2IngestionWorkflowServices` 是薄适配层：Graph 只传递 job/item/
source/content-version 标识，采集、解析、视频处理、Artifact、Obsidian、索引和版本
补偿仍由既有 Stage2Service/VideoService/IndexService 负责。

- `ProcessingJob` 继续只使用既有 queued/running/succeeded/failed/cancelled 状态；Graph
  的 review/publish 等待点记录为 `pending_review`/`pending_publish` stage，不新增业务状态。
- job UUID 同时作为系统生成的 workflow thread key；用户 URL、路径、正文和 provider
  响应不会进入 Graph state 或 checkpoint。
- 现有 `/api/items/{id}/review`、`/publish` 和 `/api/jobs/{id}/cancel` 通过同一 job 的
  checkpoint resume；重复 publish 在完成 checkpoint 上无副作用。失败从最后成功 checkpoint
  重试，生产 adapter 在 SQLite 当前/待定版本边界上先做幂等检查。
- 视频 `asr_required` 保留既有 capability-degraded 结果并结束当前 ingestion workflow，
  不伪造审核或索引成功。Obsidian watcher/rescan 仍是普通 service 的补偿入口，不复制为
  第三个 Graph。
- 应用 lifespan 显式打开/关闭独立 AsyncSqliteSaver；工作流 runtime、service/provider 和
  连接对象不进入 state。错误摘要仅在 JobRunner 公共诊断边界截断、脱敏且拒绝 traceback。
- review/publish 的 reject/cancel 由适配层委托 Stage2 application service：未发布候选清除
  pending 并转为 `failed`，已发布条目保持 current/published；新审核周期走现有重新提交或
  reprocess，不删除 Obsidian Markdown。

## 8.3 阶段 6 批次 C：生产 QuestionAnswerGraph 编排

`QuestionAnswerGraph` 的生产入口是 `QuestionAnswerWorkflowCoordinator`。它先用
`QuestionAnswerInput` 在 Graph 调用前完成查询脱敏、长度和 UUID 校验，再以请求 UUID 同时作为
系统拥有的 workflow request/thread key。Graph 的 `retrieve` 节点调用现有
`QuestionAnswerService.answer()` 原子服务；`answer` 节点只把已完成结果投影为
`model_run_id`/`citation_ids`，不在 Graph 中拆写检索、证据、引用或 Provider 业务规则。

- `/api/chat/stream` 继续由同一 `AnswerResult` 产生 `meta`、`delta`、`citations`、`done` 四类
  SSE 事件。API 的 `limit`、rewrite 和 source type 只作为受控参数保存在 workflow request，
  不进入 Graph state；request 同时保存脱敏 query 哈希、mode 和 options fingerprint。
- `workflow_requests` 是 migration 0005 新增的真实幂等边界，主键为 canonical UUID，保存受控
  参数、状态、稳定 error code 和最小结果快照。成功时 ModelRun 状态、Citation 快照和 request
  终态在一个 SQLite 事务内提交，覆盖“数据库已提交而 checkpoint 尚未推进”的窗口。
- 重复 request、Graph 重编译和新 checkpoint runtime 会先读取 workflow request；成功结果从
  ModelRun/Citation 重新验证，重新检查 SQLite 的 published、非删除、current content version
  与内容哈希，任何不一致都 fail closed。恢复逻辑不把答案正文、Chunk 全文、Embedding 或
  Provider 原始响应写进 state/checkpoint。
- 证据状态不是 `sufficient` 时直接进入 `refuse`，不调用答案 Provider；生产 RAG 原子边界仍
  完全由既有 QuestionAnswerService、HybridRetriever、EvidencePolicy、CitationBuilder 和
  Provider 负责。该批次没有引入第三个 Graph、Agent 循环或第二套问答业务逻辑。
- 相同 request ID 但 fingerprint 不同的调用在检索/Provider 前返回稳定 `idempotency_conflict`；
  完整 claim/execute/finish 区间由共享 mutation lock 串行化，避免并发 ModelRun/Citation 重复。

## 8.4 阶段 6 批次 D：受控 MCP Server（Provider）

MCP Provider 端实现位于 `backend/app/mcp/`，使用锁定的标准 Python MCP SDK
`mcp==2.1.1`。`MCPKnowledgeServer` 只注册五个工具：`add_text`、`add_url`、
`search_knowledge`、`get_item` 和 `list_collections`；stdio 模式下 stdout 只承载协议帧，
不把日志或错误文本写入协议流。

- 五个工具都调用共享的 `KnowledgeApplicationService`。Stage2 提交、SSRF/DNS/redirect
  校验、SQLite 权威检索、条目路径授权和 Collection 查询仍由普通 application service 或
  既有 service 负责，MCP 层只做边界适配和安全结果投影。
- 工具输入在 SDK coercion 之前通过 strict Pydantic `extra=forbid` 模型校验；未知字段、
  非法 UUID、Windows 绝对路径、loopback/私网 URL 和超大结果均 fail closed。错误只暴露稳定
  `mcp_*` code，结果经过专用递归脱敏和响应大小限制，不保存或返回 API key、Authorization/Cookie、
  traceback 或真实绝对路径；`get_item` 只投影已发布且没有 pending candidate 的安全正文，正常
  `body` 不再被错误详情策略整体抹除。
- 由于 PROJECT.md 要求 Collection 实体，而现有 schema 没有该关系，0006 只增加
  `collections`、`collection_items` 及必要索引，并验证可逆 upgrade/downgrade/upgrade；
  MCP 不拥有 checkpoint schema，也不引入 MCP Client、Agent 循环或未经配置的外部连接。

## 8.5 阶段 6 批次 E：受控 MCP Client（Consumer）

`ControlledMCPClient` 只接受结构化 `MCPServerProfile`，并且只有在调用方显式选择 profile
后才建立一次连接。当前 transport 只实现 stdio；profile 中的 `entrypoint_id` 仅引用由可信
应用代码注册的 `TrustedStdioEntrypoint`，不允许用户配置 command、args、cwd、shell、原始
endpoint 或认证 secret。内存 transport 只作为测试注入点，不属于用户配置能力。

- 建连时先校验远端 initialize 的 server name，再以超时读取 `tools/list`。客户端把远端工具
  与 profile 能力白名单取交集；未授权工具不能调用，工具输入 schema 必须是受限的 JSON Schema
  安全子集，拒绝 `$ref`、开放 `additionalProperties`、过深/过大 schema 和异常分页。
- 每次调用在发送前检查 JSON 原语、请求大小、严格 object schema 和额外字段；调用由
  `ClientSession` 的 async context 与硬超时托管。远端错误、断线和取消不把异常文本传播给上层，
  只产生稳定 client error code；退出 context 会关闭 session 与 stdio transport。
- 结果不会进入 Graph state 或 checkpoint。客户端将 SDK 结果投影为限额内 JSON，并递归处理
  API key、Authorization、Cookie、token、traceback、命令和绝对路径等敏感字段；异常结果和
  过大响应 fail closed。批次 E 不加入 Agent 自动循环，也不在主 API 装配中自动连接任何 server。

## 8.6 阶段 6 批次 F：备份、恢复与派生状态重建

`BackupRestoreService` 位于普通 application service 层，不是第三个 Graph。它使用固定归档布局：
`manifest.json`、业务 `data/business.sqlite`、可选的独立 `data/checkpoint.sqlite`、受管理的
`artifacts/` 和 `vault/`；manifest 只包含归档内相对成员、角色、大小和 SHA-256。真实绝对 Vault、
Artifact、数据库或 checkpoint 路径不会进入 manifest、日志、API 或 state。checkpoint 的内部
`checkpoints/writes` schema 仍由 LangGraph 依赖自管；备份服务只对其做完整性检查，不混入业务
Alembic migration。

- 业务 SQLite 使用 SQLite 在线 backup API 创建一致性快照，并校验当前 migration head；checkpoint
  同样快照并校验 integrity。归档不包含 Qdrant，manifest 明确标记其为需重建的派生存储。
- 创建与恢复都拒绝绝对路径之外的推导、`..`、符号链接、重复/目录归档成员和超大归档。restore
  只接受调用方给出的互不包含的绝对目标，并要求离线短生命周期进程；已有目标必须显式
  `allow_overwrite`。业务 SQLite/checkpoint 主文件及各自 `-wal/-shm` 与 Artifact/Vault 覆盖安装
  先将旧目标移到带随机 token 的临时备份，任何中断或清理失败都回滚已安装目标与旧目标，成功
  不继承旧 sidecar。归档不得与数据库/checkpoint 主文件或 sidecar 重叠，也不得位于 Artifact/Vault
  目标内；实际 staging 创建后还会检查恢复目标与 staging 的父子路径碰撞。
- `scripts/zhiliutai_backup.py` 是最小用户入口，backup/restore/rebuild 的所有目标都必须显式为
  绝对路径，restore 默认不覆盖，也不从 manifest 推导路径；恢复前必须停止 API、JobRunner、
  watcher 和 checkpoint 使用者。
- `rebuild_derived_state` 先核对 Artifact 哈希、Obsidian 受管理相对路径、`zhiliu_id`、当前
  ContentVersion/KnowledgeItem 内容哈希和 published/non-deleted/current 关系，全部通过后才清空
  Chunk/FTS5/Qdrant 并复用 `IndexService` 重建。Qdrant 仍不是版本权威，验证失败不会先清空旧派生物。
- 空环境只启动本机服务；未配置 Vault 或模型时 health 返回可诊断的 `degraded` 与
  `not_configured`，不会暴露或尝试使用 secret。备份/恢复/重建测试只使用临时 SQLite、Qdrant、
  Artifact 和 Vault。

## 8.7 阶段 6 批次 G：合成验收与交付门禁

阶段 6 的合成闭环位于 `backend/tests/test_stage6_end_to_end.py`：它调用既有 HTTP 入口完成文本
采集、JobRunner/IngestionGraph、review/publish HITL、临时 Obsidian Markdown、SQLite/Qdrant
检索和 QuestionAnswerGraph SSE，再使用批次 D 的 MCP Server memory transport 查询同一已发布
条目。测试使用确定性 draft/embedding/chat provider，不连接真实网页、模型、Vault 或外部 MCP。
上述阶段 6 实现及本轮 P1 修复已由 Sol 完成最终独立复验，结论为 **PASS WITH NON-BLOCKING RISKS**。
本机 Playwright runner 的挂起、真实外部互操作和跨存储物理事务仍按非阻塞风险登记，不能把未完成
的本机浏览器执行写成通过。

前端 Playwright 位于 `frontend/tests/e2e/`，只验证浏览器呈现与既有 API 边界，不复制业务逻辑：
它在固定 `workers=1` 的 127.0.0.1 Vite 服务上用稳定的浏览器内 API fixture 覆盖收件箱提交、
人工审核、发布到 Obsidian、搜索、证据约束回答和 Citation 卡片。配置保留失败 trace、截图和
视频；GitHub Actions 在独立 job 中安装锁定版本的 Chromium、运行 E2E typecheck/Playwright 并
始终上传 `playwright-report`/`test-results`。本机 runner 在收集阶段未完成，不能把本地限制解释成
浏览器通过。

## 9. Health

`GET /api/health` 检查：

- FastAPI
- SQLite（打开、查询、WAL）
- Qdrant Local（本地路径初始化）
- Artifact Storage（实际读写）
- Obsidian Vault
- Obsidian Watcher
- Model Providers
- FFmpeg

状态统一为 `healthy/degraded/not_configured/configured/unavailable`。Vault 或模型未配置不会伪装正常；FFmpeg 缺失只影响后续视频能力，不拖垮 API。Request ID、404/422/500 统一错误形状和单次结构化异常日志继续保留。

Chat、Embedding、ASR、Vision 和 Reranker 的 key 只来自后端 Settings。Health 探测不跟随
重定向，拒绝带用户凭据、查询串或片段的模型 endpoint，只在请求头中发送 Authorization，
且不会把 key 写入 detail、日志或响应。

## 10. Docker、CI 与浏览器

Dockerfile 构建 React 后由 FastAPI 托管静态产物，容器启动时执行 SQLite migration。Docker 是 delivery/CI concern，不是本地 prerequisite。`compose.yaml` 已移除。

GitHub Actions 分别执行后端锁定同步、Ruff、pytest、同一临时 SQLite 的 upgrade/downgrade/upgrade，前端 npm ci/typecheck/test/build、Playwright E2E typecheck/Chromium 安装/执行与失败产物上传，以及 Docker build。当前机器没有 Docker，所以本地没有把 Docker build 标记为通过。

组件自动化使用 Vitest + Testing Library + jsdom。真实浏览器 E2E 将由 Playwright CI 或浏览器能力正常的环境执行；当前 runner 故障不阻塞阶段推进，也不记为通过。
