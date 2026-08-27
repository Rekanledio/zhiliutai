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
