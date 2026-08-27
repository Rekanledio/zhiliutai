# 项目状态

更新时间：2026-08-27

## 阶段状态

- 阶段 0：**通过**。
- 阶段 1：**通过**。
- 阶段 2：**通过自动化与人工闭环验收**。
- 阶段 3：**通过自动化闭环验收**。
- 下一阶段：阶段 4（自建 RAG），尚未开始。

阶段 1 的验收定义已按 ADR-0011 收口：本地不再要求 Docker Desktop、PostgreSQL、pgvector、Redis 或当前 runner 的真实浏览器。Docker build 和 Playwright E2E 分别是 CI/具备能力环境的门禁，未执行即不标记 passed。

## 架构收口

- `PostgreSQL + pgvector + Redis + Celery + Docker Compose` 已替换为 `SQLite + SQLite FTS5 + Qdrant Local + Python JobRunner`。
- `compose.yaml`、旧生产依赖和 PostgreSQL-only migration 已删除；`uv.lock` 已更新。
- SQLite baseline 创建阶段 2 实际使用的实体和 `chunk_fts`；临时数据库 upgrade/down/up 通过。
- Dockerfile、`.dockerignore` 与 GitHub Actions 已加入。当前机器没有 Docker，所以 Docker build 只登记为 CI 待执行能力，不登记为本地通过。
- Health 已切换为 FastAPI、SQLite、Qdrant、Artifact、Obsidian、Watcher、Model Providers 和 FFmpeg。

## 阶段 2 已实现

- Text/Markdown 规范化、SHA-256、内容寻址 Artifact、去重和幂等键。
- SQLite 持久化 JobRunner、JobAttempt、结构化失败、重试和进程重启恢复。
- OpenAI-compatible Chat/Embedding capability；测试使用确定性 provider。Chat 未配置时保留原文草稿，Embedding 未配置时阻止伪装完成向量发布。
- 草稿、编辑、审核、发布、软删除和既定 API。
- 稳定 Frontmatter、受管理 Vault 路径、原子 Markdown 写入和 Obsidian 深链。
- Chunk、SQLite FTS5、Qdrant payload 与发布后 searchable index。
- watcher/rescan、内容哈希、外部修改新版本、重命名、missing/conflict 和重索引。
- 多次快速保存保留完整 `ContentVersion` 历史，但 Chunk、FTS5 与 Qdrant 只保留当前发布版本；瞬态不完整 Markdown 延后处理并回退到最近有效版本。
- Inbox、Knowledge、Jobs 页面；前端 Request ID、统一 API 错误、超时和 AbortController 取消。

## 阶段 3 已实现

- 统一 SourceBlock/ParsedSource 管线接入 PDF、DOCX 和无需登录的静态 HTML。
- PDF 保留页码，DOCX 保留标题层级和表格行定位，网页保留最终 URL 与标题层级。
- 文件原始 Artifact、网页 URL 请求 Artifact 与 HTML 快照 Artifact 均内容寻址保存。
- URL scheme、凭据、DNS 私网/环回、重定向、响应类型、大小和超时边界已实现。
- ContentVersion.source_metadata_json 保存可重建来源 segments；发布后的 Chunk、
  SQLite FTS5 和 Qdrant payload 复用结构化 locator。
- 阶段 3 仍复用现有 JobRunner、草稿/审核/发布、Vault 和当前版本索引收敛，
  未引入 RAG Chat、视频、Agent 或 MCP。

人工闭环已在专用测试 Vault 验证：Markdown 提交、真实 DeepSeek 草稿、审核、发布、Obsidian 打开、连续多次外部修改、watcher 重扫与 FastEmbed/Qdrant 重索引均成功。最终只读核验为 SQLite Chunk 1 个版本、FTS5 1 个版本、Qdrant 1 个当前版本点，三者与 `KnowledgeItem.current_content_version_id` 一致。

## 当前验证结果

~~~text
uv sync --project backend --locked                    passed
uv lock --project backend --check                     passed
uv run --directory backend ruff check app tests       passed
uv run --directory backend pytest -q                  31 passed
uv run --directory backend pytest -q tests/test_source_pipeline.py  5 passed
temporary SQLite alembic up/down/up                    passed
npm --prefix frontend ci --ignore-scripts ...         passed
npm --prefix frontend run typecheck                   passed
npm --prefix frontend run test                        9 passed
npm --prefix frontend run build                       passed
FastAPI 127.0.0.1 runtime + GET /api/health            passed
Vite 127.0.0.1 runtime                                passed
manual Stage 2 dedicated-Vault workflow               passed
~~~

自动化测试全部使用临时 SQLite、Qdrant、Artifact 与 Vault；未使用真实个人 Vault 或真实 API Key。人工验收使用专用测试 Vault 和仅保存在本机 `.env` 的 provider 配置。

## Git 与数据边界

- `.env`、SQLite、Qdrant、Artifact、Vault、`node_modules`、虚拟环境和构建缓存均被忽略。
- 阶段 2 收尾 commit 为 `b4b1fd5`，已成功推送到 GitHub `origin/main`；阶段 3
  修改当前仍在工作树中，按约定尚未 commit/push。

## 真实剩余项

- environment limitation：本机没有 Docker，未本地执行 `docker build`；GitHub Actions 已提供对应 gate。
- environment limitation：当前浏览器 runner 曾报 `trusted Node process exited unexpectedly; kernel reset`；automated component verification passed，real browser E2E pending Playwright/browser-capable environment。
- future phase：FFmpeg 当前不可用，阶段 5 视频功能前再验证，不自行安装。
- future phase：Hybrid RAG、RAG Eval、视频、LangGraph、Agent、MCP Server/Client 仍按 `PROJECT.md` 后续阶段实施。
- blocking：无。
