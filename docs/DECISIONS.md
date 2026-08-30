# 决策日志

## ADR-0001：以 docs/PROJECT.md 作为单一需求依据

- 状态：已采纳
- 日期：2026-08-25
- 决策：项目需求、范围和阶段验收以 docs/PROJECT.md 为准；本文件只记录实施取舍。
- 原因：避免需求在对话、README 和实现之间漂移。

## ADR-0002：本机优先，基础设施容器化

- 状态：已被 ADR-0011 取代
- 日期：2026-08-25
- 决策：PostgreSQL/pgvector 和 Redis 的官方开发路径使用 Docker Compose；FastAPI、Worker 和 React 在本机运行。
- 原因：符合单用户本机优先设计，也降低 Windows Vault 文件监听和挂载问题。当前环境没有 Docker 命令，因此本轮只建立 Compose 基线，不宣称容器已验收。

## ADR-0003：初始化本地 Git 仓库但不提交

- 状态：已采纳
- 日期：2026-08-25
- 决策：当前目录原先不是 Git 仓库，本阶段执行 git init；不自动创建 commit 或推送。
- 原因：阶段 0 要求明确仓库边界，同时保留用户对首个提交内容的控制。

## ADR-0004：锁定 Python 与前端依赖声明

- 状态：已采纳
- 日期：2026-08-25
- 决策：后端使用 uv 管理 backend/pyproject.toml 和 backend/uv.lock；前端使用 npm 的 package.json 和 package-lock.json。首版依赖使用精确版本，运行时以 Python 3.12 和 Node 24 为基线。
- 原因：让阶段验收可复现，减少跨机器的隐式升级。锁文件只包含依赖元数据，不包含用户数据或密钥。

## ADR-0005：阶段 1 健康状态允许降级

- 状态：已采纳
- 日期：2026-08-25
- 决策：首页显示 API、PostgreSQL、Redis、Obsidian 监听器和模型服务的独立状态；依赖未启动时 API 仍可启动并明确标记 degraded，不伪造“正常”。
- 原因：首页需要从空环境可运行；真实基础设施状态必须可见，不能因开发环境缺少 Docker 或 Vault 而让前端完全不可访问。

## ADR-0006：Celery 使用兼容的 Redis Python 客户端

- 状态：已被 ADR-0011 取代
- 日期：2026-08-25
- 决策：后端将 redis Python 客户端锁定为 5.2.1，而不是 6.4.0。
- 原因：uv 解析验证显示 Celery 5.5.3 的 redis extra 约束最高到 5.2.1；继续使用 6.4.0 会让阶段 0 锁文件不可解。Redis 服务端仍使用 Compose 的 7.4 镜像，客户端与服务端版本是不同边界。

## ADR-0007：健康状态只表示已验证能力

- 状态：已采纳
- 日期：2026-08-26
- 决策：PostgreSQL 健康必须同时通过连接查询和 `vector` 扩展检查；尚未启动的 Obsidian 监听器不能因 Vault 目录存在而健康；模型服务使用不发送用户内容的 `/models` 探测，无法验证时使用 `configured`，不可达或模型不存在时使用 `degraded`。
- 原因：健康状态必须对应可验证能力，配置存在、目录存在和服务真正可用不能混为一谈。

## ADR-0008：统一请求 ID 与未处理异常日志

- 状态：已采纳
- 日期：2026-08-26
- 决策：错误处理器和正常响应都写入同一请求 ID 的 `X-Request-ID`；中间件不再为未处理异常额外记录堆栈，500 全局处理器负责唯一一次结构化异常日志。
- 原因：让 404、422、500 的响应体与响应头可关联，并避免同一异常产生重复完整堆栈。

## ADR-0009：Artifact 相对路径以仓库根为基准

- 状态：已采纳
- 日期：2026-08-26
- 决策：`ARTIFACT_ROOT` 的相对值始终相对仓库根解析；默认值为仓库根 `data/artifacts`，绝对路径仍可由本机配置显式指定。
- 原因：`uv run --directory backend` 会改变进程当前工作目录，不能让运行方式决定用户数据落盘位置。

## ADR-0010：阶段 1 前端测试采用真实 React 挂载

- 状态：已采纳
- 日期：2026-08-26
- 决策：前端测试使用 Vitest、Testing Library 和 jsdom 实际挂载 React 首页，保留 typecheck 和生产构建；浏览器级布局与 console error 仍需在可用浏览器环境复验。
- 原因：静态读取 `index.html` 不能证明 React 请求、状态、导航或交互行为。

## ADR-0011：最终单机架构采用 SQLite、Qdrant Local 与 JobRunner

- 状态：已采纳
- 日期：2026-08-26
- 取代：ADR-0002、ADR-0006，以及 ADR-0005/0007 中 PostgreSQL、pgvector、Redis 相关的健康组件。
- 决策：
  1. SQLite 是最终默认业务数据库，继续使用 SQLAlchemy 与 Alembic；启用 foreign keys、WAL、busy timeout，全文检索使用 FTS5。
  2. Qdrant Python Client 的 local persistence 是最终向量存储，不启动 Qdrant Server，也不让 Qdrant 承担业务主状态。
  3. Redis/Celery 被 SQLite `ProcessingJob`/`JobAttempt` + Python JobRunner 取代。
  4. Docker 只负责单应用交付和 CI build，不是本地开发 prerequisite；只服务旧 PostgreSQL/Redis 的 Compose 被删除。
  5. 第三方可视化 RAG 平台不进入架构；LangChain、LangGraph、RAG、Agent 与 MCP 由本项目直接实现。
  6. MCP 同时实现 Server（Provider）与 Client（Consumer）；两侧都复用 application service/Tool 边界。
- 原因：
  - 本项目是 single-user、local-first，SQLite 的事务、备份、零守护进程和 FTS5 更符合真实运行规模。
  - Qdrant Local 保留专业向量检索与 payload 追踪能力，同时消除服务运维；与 SQLite 分责比把业务数据放进向量库更清晰。
  - 持久化 Job 表保留重试、心跳、恢复和 attempt history，而无需 Redis/Celery 的额外故障面。
  - 移除本地基础设施依赖提高空环境可复现性，把复杂度预算留给 AI Engineering、引用正确性和知识同步。
  - MCP Provider + Consumer 能完整展示协议双向实践，不需要多 Agent 或另一套业务逻辑。
- 后果：不再维护 Lite/Full、PostgreSQL、Dify 或 Compose 等平行运行模式。若未来产品范围变为多用户或高并发，必须另立 ADR，不能在当前代码中预留双轨。

## ADR-0012：阶段 2 watcher 与索引切换采用单进程可恢复协议

- 状态：已采纳
- 日期：2026-08-26
- 决策：首版 watcher 运行在单个 FastAPI 进程中，轮询受管理 Markdown 并以显式 rescan 补偿漏事件；发布使用原子文件替换，索引成功后才切换 current version。
- 原因：单用户本机应用不需要分布式 watcher。稳定 `zhiliu_id`、内容哈希、幂等 rescan 和失败不切换旧版本，比引入额外守护进程更易验证且更可靠。
- 边界：当前不支持多个 API 进程同时监听同一 Vault；未来若需要多进程，必须先增加单实例协调和新的并发测试。

## ADR-0013：阶段 4 采用 SQLite 权威复核的自建 Hybrid RAG

- 状态：已采纳
- 日期：2026-08-27
- 决策：
  1. QueryProcessor 生成参数安全的 FTS5 查询；FTS5 与 Qdrant Local 并行召回，
     使用 RRF 去重融合。Qdrant payload 只做候选过滤和结构校验，published、软删除
     和 current_content_version_id 始终回到 SQLite 权威复核。
  2. EvidencePolicy 只评估最终 top-k；证据为空或置信度不足时直接拒答，不调用答案
     Provider。答案输出采用结构化 claim，每个 claim 必须绑定本次合法 Citation。
  3. CitationBuilder 只从 SQLite Chunk 和已验证的来源元数据生成 exact/fallback/
     unavailable locator 与受控 target，不猜测页码、标题或 URL。
  4. ModelRun 与 Citation 保存本次问答的 provider、Prompt、参数、Token、耗时、内容
     哈希、版本和定位快照；这些记录不是用户可编辑正文主库。
  5. Reranker 仅以可注入 Protocol 存在；当前没有明确上游 HTTP 契约，不实现生产 HTTP
     adapter，只提供本地确定性参考实现和固定中文离线评测。
  6. 阶段 4 提供普通 QuestionAnswerService 和 SSE API，不引入完整 LangGraph、
     Agent 或 MCP。
- 原因：先保证版本正确性、引用可追溯和证据不足时的安全行为，再为未来编排层保留
  清晰 service 边界；避免把向量库或未知协议当成业务权威。
- 后果：RAG 查询在单机 SQLite/Qdrant Local 上可重建；外部模型不可用时搜索仍可用，
  问答会返回安全错误或拒答。生产 reranker、Graph、Agent 和 MCP 仍需后续阶段另立
  实现与验收。

## ADR-0014：阶段 4 引用与问答审计以来源元数据和原子版本快照为准

- 状态：已采纳；阶段 4 独立复验结论为 PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-27
- 决策：
  1. CitationBuilder 必须从当前 ContentVersion.source_metadata_json、同条目的
     SourceArtifact 完整性和 synced NoteBinding 验证 PDF 页码、DOCX 结构、网页最终
     URL 与 Obsidian 相对路径；Chunk/Qdrant locator 只能作为检索字段，不能作为事实
     定位权威。验证失败时只能 fallback 或 unavailable。
  2. ModelRun 成功状态和 Citation 快照必须在共享 mutation lock 下、同一 SQLite 事务
     内基于当前 Chunk/ContentVersion/KnowledgeItem 复核后写入；版本切换不得产生旧
     版本成功记录。
  3. API、SSE、前端 target 和审计字段统一拒绝绝对/穿越路径，并在边界处清除 API key、
     绝对 Vault 路径及上游敏感响应；验证错误不保留原始 input。
  4. 固定中文评测必须使用临时 SQLite/FTS5/Qdrant HybridRetriever 实跑并报告指标；
     每次运行重建临时环境并验证结果可重复；在 PROJECT.md 没有定义硬阈值前不创建质量门槛。
  5. FTS、向量和 RRF 的并列结果使用当前权威 Chunk 的内容哈希、版本哈希、ordinal、
     locator 和正文作为稳定排序键，不使用随机 UUID 决定产品排序。
- 原因：独立验收暴露了仅相信派生 locator、最终复核与持久化分离、错误回显和静态
  评测会掩盖真实风险；这些边界必须在阶段 4 内收口，不能推迟到阶段 5。
- 后果：部分来源在元数据或本地文件不可验证时只显示摘录/回退定位；前端打开来源
  需要先通过 API 可访问性检查。阶段 5 已解除冻结，但仍必须依据 PROJECT.md 分批实施和验收。

## ADR-0015：阶段 5 批次 A 先收口视频契约与 Artifact 生命周期

- 状态：已采纳；批次 A 已实现并由 Sol 定向审查
- 日期：2026-08-27
- 决策：
  1. 视频时序统一使用严格的非负整数毫秒；每个 `TimedSpan` 必须满足
     `0 <= start_ms < end_ms`，来源时长已知时由 `ProcessingManifest` 复核所有转录、章节、
     关键帧和视觉事件。
  2. ASR/Vision 先以 provider-neutral `Protocol` 和离线确定性 fake 定义边界；没有明确
     的上游协议前不实现生产 HTTP adapter，也不在批次 A 接入下载、FFmpeg、真实模型或路由。
  3. `SourceArtifact` 通过 0004 增加 `metadata_json`、保留策略、到期时间、清理状态和
     清理时间，并建立 `(cleanup_state, retention_expires_at)` 到期查询索引；已有 Artifact
     默认永久保留，`until_expiry` 必须显式提供到期时间。`KnowledgeItem.pending_content_version_id`
     与已发布 current 指针分离，且不得指向同一版本或其他 KnowledgeItem 的版本。
  4. 0004 使用 SQLite 原生 ADD/DROP COLUMN，避免 batch 重建父表时触发外键级联删除；跨表
     pending 所有权由 0004 中显式命名的 SQLite trigger 在 KnowledgeItem INSERT/UPDATE 和
     已被引用的 ContentVersion 所有权 UPDATE 时强制执行。ORM 只能表达 FK、非空和 current/
     pending 区分等本表边界，迁移是跨表不变量的数据库权威；downgrade 会先删除 trigger。
     验证必须使用临时数据库并覆盖 upgrade/downgrade/upgrade。
  5. ASR、Vision、Reranker API key 只存在后端 Settings；health 探测禁止重定向及 URL
     凭据/查询串，仅通过 Authorization 请求头发送，不写入日志、API、Markdown、ModelRun、
     错误响应或测试快照。
- 原因：阶段 5 的后续管线依赖稳定时间戳、可重建产物和可回滚版本边界；先明确这些契约，
  可以让字幕、ASR、视觉和清理实现分别验收，同时延续 SQLite/Qdrant/Obsidian 的数据所有权。
- 后果：批次 A 为后续视频入口、字幕、受控音轨、条件视觉和 Citation 提供不可变边界；
  后续批次必须继续使用这些契约，不能绕过 pending/current 或 Artifact 保留策略。

## ADR-0016：阶段 5 视频处理按安全入口、字幕优先和可重建产物分批收口

- 状态：已采纳；批次 B-F 已实现，Sol 最终复验 PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. `/api/sources/video` 只接收 URL、标题、语言、幂等键和视觉开关。URL 安全校验复用
     `SourceFetcher`，包括 HTTP(S)、凭据/敏感参数拒绝、DNS SSRF 防护、显式重定向复核、
     超时与大小限制；不接收本地路径、Cookie、Authorization、API key 或任意 downloader
     参数。yt-dlp 只有在注入的逐跳安全网络执行器存在时才允许运行；否则 capability-degraded。
  2. `VideoSourceProvider`、`VideoDownloader`、`CommandRunner`、`AudioExtractor`、
     `ASRProvider`、`SceneDetector`、`VisionProvider` 和 `OCRProvider` 是 provider-neutral
     边界。FFmpeg adapter 通过注入 runner、固定参数、无 shell 和受控临时目录工作；yt-dlp
     还必须通过注入 `YtDlpNetworkExecutor`，由该执行器拥有每次实际连接前的逐跳校验和重定向
     控制权。适配器不从 yt-dlp 最终元数据伪造 redirect chain。没有明确能力时只提供 capability
     error 或离线 fake，不伪造兼容 Provider。
  3. 处理顺序固定为字幕优先：已有 VTT/SRT/JSON3 先严格解析为 `TranscriptSegment`；无字幕
     只标记 `asr_required`，不隐式调用 ASR。ASR 兜底必须经过受控音轨和显式 Protocol。
  4. 视觉只对显式开启且被识别为 slideshow/tutorial 的视频运行；访谈和播客跳过。所有
     transcript/chapter/keyframe/visual event 使用毫秒时间轴并在 manifest 中复核时长、顺序、
     文本安全和 Artifact 哈希。
  5. 视频结果先进入 pending `ContentVersion`；审核发布后才写 Obsidian Markdown 并重建
     Chunk、FTS5、Qdrant；发布先暂存 Vault，候选索引失败会回滚/补偿 Vault 并按候选版本清理
     Qdrant，保持 SQLite current 权威。视频 Citation 只有在 current SQLite 版本、来源/媒体/转录/关键帧
     Artifact、manifest 和时间段均验证通过时返回 exact，否则 fallback/unavailable。
     Qdrant 永远不是版本权威。
  6. 网络媒体默认 `delete_after_processing`；字幕、来源元数据、manifest 和关键帧按永久
     策略保存，`until_expiry` 由到期清理。现有 0004 已提供所需字段，本批不新增 0005。
- 原因：视频输入同时涉及 SSRF、外部命令、长时媒体、模型边界和版本一致性；把入口、获取、
  解析、视觉、发布和 Citation 分开验证，可以在无真实网页、工具、模型和密钥的条件下证明
  安全不变量与失败恢复。
- 后果：当前实现可由临时 SQLite、Artifact、MockTransport、确定性 DNS/runner/provider 和
  合成媒体复验；默认 loopback 执行器提供生产连接策略，离线 fake 不能替代真实网站和二进制
  互操作证明。视频发布的 Vault/SQLite/Qdrant 补偿和应用内 locator 已有确定性回归。

## ADR-0017：阻塞修复采用硬网络门槛、暂存发布和应用内视频定位

- 状态：已采纳实现；Sol 最终复验 PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. yt-dlp 不是逐跳 SSRF 防护组件。`YtDlpDownloader` 没有安全执行器时不得调用
     `CommandRunner`；有执行器时只传固定参数、`--ignore-config`、`--no-plugin-dirs`、受控
     loopback proxy 和隔离环境。执行器必须负责每一跳实际连接前的 URL/DNS/全部 IP 校验、
     DNS rebinding 防护、超时和跳数限制；适配器不合成重定向链。
  2. 发布协议把 Vault 正式交换放在候选 Embedding/Qdrant/SQLite 索引准备之后。交换后校验
     或提交失败时恢复 Vault 字节快照、回滚 SQLite，并按候选 `content_version_id` 删除可能
     已写入的向量；旧 current/published 与旧检索证据保持权威。
  3. 视频 Citation 通过当前版本授权的 locator API 返回字幕片段或关键帧元数据。前端在应用
     内呈现这些可重建 Artifact，不把 transcript VTT 的媒体 fragment 或已清理网络视频当作
     已验证的跳转。
- 原因：Sol 复验确认直接 yt-dlp 子进程存在内部重定向/DNS rebinding 盲区，Vault 先写会造成
  版本分叉，媒体 fragment 不能证明用户已到达时间点；上述边界把未知能力显式降级并保留
  SQLite 的阶段 4 权威规则。

## ADR-0018：yt-dlp 默认使用 loopback 安全代理和内置下载器

- 状态：已采纳实现；阶段 5 最终门禁通过
- 日期：2026-08-28
- 决策：
  1. `YtDlpDownloader` 默认创建每次调用独占的 `LoopbackYtDlpNetworkExecutor`，不再要求
     上层另外注入生产执行器。子进程仍不得直接联网，只能使用执行器给出的 loopback proxy。
  2. 代理对每个 HTTP 请求和 HTTPS `CONNECT` authority 重新解析 DNS，任一解析结果属于
     loopback、私网、链路本地、保留、文档或云元数据地址即 fail closed；连接使用同次校验
     得到的 numeric IP，不再次按 hostname 解析。监听、并发、请求总数、响应字节和超时均
     有固定上限。
  3. yt-dlp 固定 `--downloader native`，同时继续使用 `--ignore-config`、`--no-plugin-dirs`、
     scrubbed environment、固定输出目录和无 shell runner；网络下载不得委托给 ffmpeg、curl、
     aria2c 等边界外进程。FFmpeg 只处理受控目录中的本地媒体。
  4. HTTPS 隧道不做 TLS 中间人，因此不会声称能观察明文 redirect URL 或精确重定向次数。
     每个实际连接目的地仍重新执行 SSRF 校验，整体请求数和进程超时提供资源上限；若注入
     执行器能提供可信 redirect chain，适配器继续按配置跳数、首尾 URL 和逐跳安全复核。
- 原因：直接 yt-dlp 不能满足 DNS-rebinding 和私网目标拒绝；只提供外部 Protocol 又使默认
  视频入口永远 capability-degraded。loopback proxy 在不读取 TLS 内容、不传递用户 Cookie
  或配置的前提下，对实际 socket 目标实施应用自己的权威策略。
- 后果：生产默认路径具备可执行的网络安全边界，但本轮按约束没有访问真实网站或运行真实
  yt-dlp/FFmpeg；二进制版本、网站 extractor 变化及真实媒体互操作仍需在获准环境单独验证，
  不得由离线 runner 测试推断为已通过。

## ADR-0019：阶段 6 批次 A 先建立安全 Graph 契约与独立 checkpoint

- 状态：已采纳；批次 A–G 实现及本轮阻塞修复已通过 Sol 最终复验，PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. IngestionGraph 与 QuestionAnswerGraph 的 state 使用 TypedDict/Literal，外部输入、
     resume 决策和服务结果使用 extra=forbid 的 Pydantic 模型；只允许 JSON 安全的 ID、
     路由、决定、状态、稳定 error code 和 citation ID，不保存正文、Chunk、Embedding、
     Provider 原始输出、密钥或连接对象。
  2. safe_query 在第一次 graph 调用前复用既有 safety 脱敏并完成控制字符、NFKC 和长度
     校验；thread_id 由 runtime 生成或接受已验证 UUID，不放入 state。编译图入口也拒绝
     未验证的初始 state、Command update 和 resume 扩展字段。
  3. checkpoint 使用独立 AsyncSqliteSaver 文件，应用只负责显式 setup、WAL/busy timeout
     和关闭，不新增业务 migration，也不依赖 checkpoint schema 的内部字段。
  4. HITL gate 在 interrupt 前不调用副作用服务；失败恢复从最后成功 checkpoint 继续，
     重复 resume 对终态不重复调用服务。证据状态非 sufficient 时 QuestionAnswerGraph
     必须拒答且不调用答案服务。
- 原因：阶段 6 先验证状态机、恢复、HITL 幂等和安全落盘边界，再在后续批次接入生产
  service，避免 Graph 形成第二份正文主库或绕过阶段 4/5 的权威性约束。
- 后果：批次 A 定义的两个 Graph 仍是唯一编排层；生产 service、MCP、备份恢复和端到端
  实现继续复用既有边界，不形成第二套正文或业务规则。阶段 6 最终结论为 PASS WITH NON-BLOCKING RISKS。

## ADR-0020：阶段 6 批次 C 用 durable workflow request 保证 QA Graph 幂等

- 状态：已采纳；本轮 QA 幂等阻塞修复已通过 Sol 最终复验，PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. `QuestionAnswerGraph` 只编排现有 `QuestionAnswerService.answer()` 原子 application
     service。生产 adapter 在 retrieve 节点触发一次原子服务，answer 节点只投影安全的
     ModelRun/Citation 标识；不在 Graph 中复制检索、证据门禁、引用或 Provider 逻辑。
  2. 新增 `workflow_requests` 表（0005）作为 canonical UUID 请求的 durable idempotency
     boundary。受控参数、脱敏 query 的稳定哈希、mode、limit、rewrite、source_types、状态、
     稳定错误码和最小结果快照由 SQLite 保存；ModelRun、Citation 与成功 request 状态在一个
     事务内提交，解决数据库已提交而 checkpoint 尚未推进的崩溃窗口。
  3. 相同 fingerprint 的重复 request 或新 runtime 只恢复已有安全结果，并重新校验 Citation
     所属 ModelRun、当前 published/非删除/current ContentVersion、Chunk 与版本内容哈希；同
     request ID 的 query/mode/options fingerprint 不同则在检索或 Provider 前返回稳定冲突并拒绝。
  4. 证据不是 sufficient 时走 Graph refuse 分支，答案 Provider 调用必须为零。相同 request 的
     claim/execute/finish 状态转换由完整 mutation lock 保护，避免并发重复 ModelRun/Citation。
     现有 chat SSE
     的事件顺序与字段保持兼容；request options 不进入 Graph state，state/checkpoint 不保存
     Chunk 全文、证据正文、Embedding 或 Provider 原始响应。
- 原因：LangGraph checkpoint 与业务 SQLite 不是同一事务，单靠“节点完成”无法覆盖提交后崩溃；
  请求主键和同事务结果引用让重放可验证、可幂等，同时延续 SQLite/Qdrant/Obsidian 的数据所有权。
- 后果：重复问答 request 会复用同一 ModelRun/Citation；失败 request 需要新 UUID 重新发起，
  不允许用相同身份绕过已记录的失败边界。进程级不可捕获崩溃仍需依赖新 runtime 重新打开
  checkpointer，真实 Provider/网络互操作不在本批测试范围。

## ADR-0021：阶段 6 批次 D 以共享 service 和严格 stdio MCP 边界提供五个工具

- 状态：已采纳；本轮 MCP 响应边界修复已通过 Sol 最终复验，PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. MCP Provider 端固定使用锁定的标准 Python MCP SDK `mcp==2.1.1`，首版只暴露
     `add_text`、`add_url`、`search_knowledge`、`get_item`、`list_collections` 五个工具，
     stdio stdout 只输出协议帧。MCP 适配层不复制 Stage2、检索、路径授权或 Collection 业务规则。
  2. 五个工具统一调用 `KnowledgeApplicationService`；输入在 SDK coercion 前通过 strict
     Pydantic `extra=forbid` 边界模型校验，输出只允许安全 JSON 原语、稳定错误码和受控相对标识，
     并设定响应大小上限。
  3. `add_url` 继续复用既有 SSRF、DNS、redirect、超时和大小边界；`get_item` 继续复用相对
     路径 containment，并只投影已发布且无 pending candidate 的正文。错误详情脱敏与正常知识
     输出使用不同策略：Cookie/Set-Cookie、Authorization、密钥、真实绝对路径、traceback 和
     完整上游敏感响应不得出现在任何字符串字段，但允许的 `body` 不被整字段删除。MCP Server
     不接受模型临时指定 server、command、路径或工具。
  4. 因 PROJECT.md 要求 Collection 实体而现有 schema 缺少该关系，0006 最小增加
     `collections`/`collection_items` 及索引，并执行临时库 upgrade/downgrade/upgrade；不把
     MCP/checkpoint 表混入业务 migration 边界。
- 原因：MCP 是外部信任边界，必须复用已验证的 application service，并在协议解析前收紧输入、
  错误和响应；Collection 是五工具 `list_collections` 的真实数据依赖，不能用内存或 MCP 专属
  伪模型替代。
- 后果：批次 D 提供可测试的 Provider 端 stdio/MCP 边界；显式配置的 MCP Client、超时/能力
  交集和生命周期仍留给批次 E，备份恢复与端到端闭环仍留给后续批次。

## ADR-0022：阶段 6 批次 E 只允许显式 profile 选择的 MCP Consumer

- 状态：已采纳；实现保留，本轮修复已通过 Sol 最终复验，PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. `MCPClientConfiguration`/`MCPServerProfile` 是唯一配置边界，包含 server 身份、transport、
     受信 entrypoint ID、能力白名单、连接/调用超时和请求/响应大小。当前只支持 stdio；endpoint、
     command、args、cwd、shell 和 secret 不接受来自配置。stdio command 只能由应用代码注册的
     `TrustedStdioEntrypoint` 提供；加载配置不会自动连接 server。
  2. `ControlledMCPClient` 建连后校验 initialize 的 server name，并将 `tools/list` 与 profile
     allowlist 取交集；仅允许严格 JSON Schema 子集，拒绝 `$ref`、开放 additional properties、
     超大/过深 schema 和异常分页。调用发送前再次执行 JSON 原语、大小、类型和额外字段校验，
     不加入 Agent 自动工具循环。
  3. ClientSession 与 stdio transport 使用显式 async context；所有连接、tools/list 和 tool call
     有硬超时，取消保持可取消语义，断线/远端异常/超大响应只产生稳定脱敏错误码。结果递归处理
     API key、Authorization、Cookie、token、traceback、命令和绝对路径，不把 SDK 对象或原始
     异常文本传播到上层。
  4. HTTP transport 本批不实现；因此不接受 endpoint，也不宣称已验证 HTTP redirect/DNS
     rebinding 策略。后续若启用，必须另设 loopback/显式 endpoint 和每跳 DNS 校验边界，不能通过
     本批 stdio profile 绕过。
- 原因：MCP Consumer 会把不可信 server 的 schema、参数和响应带入应用；先固定可信连接入口、
  最小能力和 JSON 安全子集，可以阻断模型临时指定 command/server/tool 及异常响应泄露。
- 后果：批次 E 提供可复用的受控一次性 Consumer 连接，但不提供开放 server 注册、HTTP 网络
  连接或自主 Agent loop；备份恢复、端到端闭环和 UI/CI 收口仍留给后续批次。

## ADR-0023：阶段 6 批次 F 使用固定归档、显式 restore 与可验证派生重建

- 状态：已采纳；restore WAL/SHM、归档路径边界与离线 CLI 修复已通过 Sol 最终复验，PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. 备份归档固定包含业务 SQLite 一致性快照、可选独立 checkpoint SQLite、受管理 Artifact/Vault
     文件和相对成员 manifest；manifest 只记录角色、大小和哈希。Qdrant 作为可重建派生物不归档，
     不改变 SQLite current/published/非删除权威，也不将 checkpoint 表放入 Alembic 业务 schema。
  2. 创建与 restore 只接受受控绝对目标，拒绝路径穿越、符号链接、重复成员、超大/损坏归档和
     schema 不兼容。restore 必须由离线短生命周期进程执行；已有目标默认拒绝，显式覆盖时业务
     SQLite/checkpoint 主文件及 `-wal/-shm` sidecar 与 Artifact/Vault 旧目标先移入随机临时备份，
     安装或清理失败按同一协议回滚，不能静默留下半套恢复结果。归档不得与数据库/checkpoint
     主文件、sidecar 或 Artifact/Vault 目标重叠；实际 staging 创建后也必须无目标父子碰撞。
  3. 派生重建先验证 Artifact 哈希、Obsidian 受管理相对路径、Markdown `zhiliu_id`、内容哈希和
     SQLite published/current/非删除关系，验证全部通过后才清理 Chunk/FTS5/Qdrant，并复用现有
     `IndexService`；验证阶段失败不得先破坏可用派生状态。
  4. 空环境允许 API 以安全 degraded 状态运行；未配置 Vault/模型只报告稳定诊断，不读取真实 secret
     或真实路径。所有 backup/restore/rebuild 测试只使用临时运行根与确定性 provider。
- 原因：SQLite、Artifact、Obsidian 和 Qdrant 没有跨存储物理事务，必须显式区分可恢复原件/关系
  数据与可重建向量，并在恢复前后验证所有权威哈希；固定布局和回滚协议可阻断归档注入、路径越界
  与半恢复状态。
- 后果：恢复后需要显式调用派生重建，Qdrant 不会因归档缺少而被误当作版本来源；进程级断电仍不
  形成跨存储原子提交保证，需依赖重启后的校验/重建流程。阶段 6 的端到端、Playwright 和 CI
  收口已完成，浏览器 runner 限制仍作为非阻塞风险登记。

## ADR-0024：阶段 6 批次 G 用合成闭环和受限 Playwright 完成交付收口

- 状态：已采纳；实现保留并已通过 Sol 最终复验，PASS WITH NON-BLOCKING RISKS；本机浏览器限制仍按环境登记
- 日期：2026-08-28
- 决策：
  1. 完整闭环测试必须通过真实的本地 application service/API 边界串起采集、JobRunner/
     IngestionGraph、review/publish HITL、Obsidian Markdown、SQLite/Qdrant 检索、QuestionAnswerGraph
     回答和 MCP 查询；输入、Vault、数据库、向量和 provider 全部是临时/确定性 fixture，不以 mock
     取代后端业务闭环，也不访问真实资源。
  2. 浏览器门禁使用精确锁定的 `@playwright/test==1.53.0`，固定 `workers=1` 与 127.0.0.1 Vite
     webServer；页面只通过合成 API fixture 验证现有 UI 关键流程，不能在 E2E 中新增第二套业务规则。
     失败保留 trace、screenshot、video，CI 安装 Chromium 后上传产物。
  3. CI 必须覆盖 backend locked sync/Ruff/pytest、同一临时 SQLite 的 upgrade/downgrade/upgrade、
     frontend npm ci/typecheck/test/build、Playwright typecheck/install/run 和既有 Docker delivery gate。
     本机 runner 若无法完成测试收集，只记录真实环境限制，不标记为 passed。
  4. 收口文档只描述两个 Graph、HITL/checkpoint、MCP trust boundary、备份恢复和已知风险；不为
     文档添加未实现 Agent loop、开放 MCP、第二正文主库或其他产品功能。
- 原因：阶段 6 的一致性风险跨越 Graph、Obsidian、关系数据、向量派生物和 MCP 边界，单独的单元
  测试不能证明闭环；受限浏览器 fixture 可验证 UI 事件顺序和引用呈现，同时避免真实外部依赖。
- 后果：CI/具备浏览器能力的环境承担真实 Playwright 执行；当前 Windows runner 的挂起只影响本地
  浏览器证据，不改变已通过的合成后端和前端静态/组件门禁，不能被误报为浏览器互操作通过。

## ADR-0025：阶段 6 最终阻塞修复保持安全边界

- 状态：修复完成并已通过 Sol 最终复验；阶段 6 结论为 PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-28
- 决策：
  1. QA `workflow_requests` 的幂等身份固定绑定脱敏 query 哈希、mode 和所有影响结果的受控
     options；不同身份的复用请求返回稳定 `idempotency_conflict`，相同身份的完整执行区间由
     共享 mutation lock 串行化，业务库已提交而 checkpoint 未推进时仍从业务结果恢复。
  2. IngestionGraph 的 review/publish reject/cancel 委托 Stage2 application service：未发布候选
     清除 pending 并转为 `failed`，已发布条目保留 current/published；不删除 Obsidian Markdown，
     新审核周期只能走现有重新提交/reprocess 路径。
  3. MCP 正常结果使用专用递归 sanitizer，错误字段策略不再抹除 `body`；get_item 只投影已发布
     且无 pending candidate 的内容，Cookie/Set-Cookie、Authorization、secret、绝对路径和
     traceback 在字符串字段中脱敏。
  4. restore 是离线操作；业务/独立 checkpoint 的主文件与 `-wal/-shm` sidecar 同时覆盖、清理和
     回滚，路径交叠 fail closed。`scripts/zhiliutai_backup.py` 只接受显式绝对目标，默认不覆盖。
  5. 本轮不新增 migration；未修改 PROJECT.md、frontend、API 路由装配、真实 Vault/外部服务或
     第三个 Graph。
- 原因：Sol 的独立总审查证明上述边界存在可复现的跨请求污染、HITL 状态卡死、MCP 泄露和恢复
  不一致风险；修复必须收紧现有 service/adapter 边界，而不是增加第二套业务逻辑。
- 后果：本机 Playwright runner、真实 Provider/网络/视频互操作和进程级跨存储物理原子性继续是
  明确的非阻塞风险或环境限制；未将未完成的浏览器执行标记为通过。

## ADR-0026：用户批准的可选生产 Provider 采用本地优先与严格图像边界

- 状态：已采纳；2026-08-30 完成本机运行验证
- 日期：2026-08-30
- 决策：
  1. ASR 使用项目依赖内锁定的 `faster-whisper==1.2.1` 与 `medium` 模型，懒加载并串行执行；
     `auto` 优先 CUDA `int8_float16`，初始化或推理失败才在同一请求内退回 CPU `int8`。模型缓存
     固定在被 Git 忽略的项目数据目录，应用启动和 health 不触发下载。
  2. Vision 使用 DeepSeek `deepseek-v4-flash-vision-exp` 的 OpenAI-compatible 图像接口，只接收
     `VideoService` 提供的关键帧 bytes 并编码为 Base64 data URL；HTTP 禁止重定向和环境代理，
     有请求/图像大小与超时上限，响应按严格 JSON 解析。不得接受任意远程图片 URL、本地路径，
     也不得记录 API Key 或完整上游响应。
  3. 关键帧由现有 FFmpeg 无 shell 固定参数在 Artifact 根内的临时目录按有界间隔生成；默认最多
     24 帧。路径在执行前后均复核位于受管根内，输出只作为可重建 Artifact。
  4. Reranker 使用 `sentence-transformers==6.0.0` 和 CPU 上的
     `BAAI/bge-reranker-v2-m3`，懒加载、串行运行；加载或推理失败继续由既有
     `HybridRetriever` 捕获并保留 RRF 顺序，不改变 SQLite 对 published/current/非删除状态的
     最终复核，也不改变证据不足时 Provider 前拒答。
  5. 三项生产适配器只在配置完整时由 `create_app` 注入现有普通 service，不新增 Graph、业务
     migration、正文来源或第二套检索/视频逻辑。本地 Provider 的设置状态不伪装成需要 API Key
     的远程 Provider。
- 原因：用户明确要求在阶段 6 通过后启用可选 ASR、Vision 与 Reranker；先前 ADR 中“协议未选定”
  的前提已由明确模型和接口选择替代。实现仍需保持本地优先、秘密隔离、路径授权和可安全降级。
- 后果：自动化继续只使用 injected loader、MockTransport、合成 bytes 和临时目录；真实模型运行
  属于本机人工维护验证。Vision 会将明确启用的视频关键帧发送给 DeepSeek，用户应将其视为外部
  数据处理边界；模型质量、费用、供应商可用性和首次加载耗时不是跨存储一致性保证。

## ADR-0027：收尾产品面复用现有服务边界并以 0007 保存审核建议

- 状态：已采纳、完成实现并通过 Sol 独立复验；结论为 PASS WITH NON-BLOCKING RISKS
- 日期：2026-08-30
- 决策：
  1. 收件箱的文本、Markdown、PDF、DOCX、静态网页和视频入口继续调用现有 source API；浏览器只
     读取 File bytes/text 或发送 multipart，不接受任意本地路径。批量文件队列在客户端做即时类型/
     大小校验，逐项提交、取消、保留失败项并显示重复结果。
  2. 0007 在现有 migration head 0006 之后增加 `Tag`、`KnowledgeItemTag` 和
     `ContentVersion.suggested_collections_json`。AI 标签/合集只存候选建议；审核编辑复用
     `Stage2Service`，发布成功后才写正式关系和 Markdown Frontmatter，rescan 再以 Markdown 收敛。
     不把正文复制到 SQLite，也不修改既有 migration 历史。
  3. 审核、发布、拒绝和取消继续经由 `IngestionWorkflowCoordinator` 的 interrupt/resume；重复
     已完成决策返回已有状态，不重复生成 current 版本。已发布 current 版本的重处理拒绝/取消不
     删除正文或旧 current。
  4. 知识库编辑使用既有 PATCH service 的 `expected_content_hash` 冲突保护，软删除只改业务状态
     和派生索引，不删除 Obsidian Markdown。Dashboard/Jobs 只投影安全字段，包括 JobAttempt、
     心跳、生命周期和脱敏错误摘要；不返回 payload、URL 查询串、路径、密钥或 traceback。
  5. Playwright 只使用浏览器内合成 API fixture；所有未处理的 `/api/**` 请求立即失败。本轮覆盖
     6 个场景，CI 先执行清单再安装 Chromium 和运行测试；本机缺浏览器时不把启动失败标记为通过。
- 原因：产品基线要求正文、发布、检索和安全边界保持单一权威来源；最小 0007 只补足组织元数据和
  审核候选字段，页面收尾通过既有 application service 与 API 维持同一套幂等、路径和秘密约束。
- 后果：标签/合集正式关系依赖人工确认，建议字段可随版本重建；本轮本机真实 Playwright 仍受
  Chromium 未安装限制，CI 或具备浏览器的环境必须重新执行 6 个场景。
