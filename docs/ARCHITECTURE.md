# 知流台当前最终架构

> 本文只描述当前真实架构，不保留 Lite/Full、PostgreSQL 版或第三方平台版等平行方案。稳定产品范围以 `docs/PROJECT.md` 为准。

## 1. 运行拓扑

~~~text
React + TypeScript + Vite
          │ REST / JSON（后续 SSE）
          ▼
FastAPI
 ├─ Sources / Items / Review / Jobs / Obsidian
 ├─ Python JobRunner
 ├─ Obsidian polling watcher + rescan
 ├─ OpenAI-compatible capability adapters
 ├─ 后续 IngestionGraph / QuestionAnswerGraph
 ├─ 后续 MCP Server + MCP Client
 │
 ├─ SQLite + FTS5
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
| 业务元数据、版本、任务、Chunk、ModelRun | SQLite | `data/zhiliutai.db`，SQLAlchemy + Alembic |
| 全文索引 | SQLite FTS5 | `chunk_fts`，从 Chunk/Markdown 可重建 |
| 向量索引 | Qdrant Local | `data/qdrant/`，不保存业务主状态 |

SQLite 默认启用 foreign keys、WAL 和 busy timeout。测试使用临时 SQLite 并执行真实 Alembic migration。Qdrant payload 包含 `chunk_id`、`knowledge_item_id`、`content_version_id`、`source_type`、`source_locator`、`embedding_model` 和 `embedding_version`。

Chat 与 Embedding 是独立 capability。默认 Chat adapter 使用 OpenAI-compatible API；默认 Embedding adapter 使用进程内 FastEmbed 的中文 `BAAI/bge-small-zh-v1.5`（512 维），模型缓存位于 `data/models/fastembed/`。远程 OpenAI-compatible Embedding 仍是受支持的显式配置选项。

## 3. 当前领域模型

阶段 2 已建立并实际使用：

- `KnowledgeItem`：逻辑条目、状态、软删除和当前版本指针。
- `SourceArtifact`：不可变原始输入及内容哈希。
- `ContentVersion`：草稿/Vault 派生版本、摘要、标签建议和 Prompt 版本。
- `NoteBinding`：`zhiliu_id`、Vault 相对路径、内容哈希与同步状态。
- `Chunk`：SQLite 中的可追踪文本与引用定位；向量写入 Qdrant。
- `ProcessingJob` / `JobAttempt`：持久状态、进度、心跳、重试、结构化错误和失败历史。
- `ModelRun`：为模型调用可观测性建立的基线表。

`Tag`、`Collection`、`Citation` 会在对应后续业务阶段落地，不创建无实际用途的空表；这些核心概念仍由 `PROJECT.md` 保留。

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

## 6. Provider 与 Graph 边界

Chat、Embedding、ASR、Vision、Reranker 是独立 capability。阶段 2 使用 Chat 与 Embedding；测试注入确定性 provider，不读取真实 secret。

LangChain 只用于文档、切分、Embedding、Retriever、Prompt/Message、LLM 和 Tool 等合适抽象。最终两个主要 LangGraph 是 `IngestionGraph` 与 `QuestionAnswerGraph`；Graph 负责编排、路由、条件分支和需要的 checkpoint/HITL，业务逻辑留在普通 service。

## 7. Health

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

## 8. Docker、CI 与浏览器

Dockerfile 构建 React 后由 FastAPI 托管静态产物，容器启动时执行 SQLite migration。Docker 是 delivery/CI concern，不是本地 prerequisite。`compose.yaml` 已移除。

GitHub Actions 分别执行后端锁定同步、Ruff、pytest、SQLite migration，前端 npm ci/typecheck/test/build，以及 Docker build。当前机器没有 Docker，所以本地没有把 Docker build 标记为通过。

组件自动化使用 Vitest + Testing Library + jsdom。真实浏览器 E2E 将由 Playwright CI 或浏览器能力正常的环境执行；当前 runner 故障不阻塞阶段推进，也不记为通过。
