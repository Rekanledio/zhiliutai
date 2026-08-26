# 项目状态

更新时间：2026-08-26

## 阶段状态

- 阶段 0：**通过**。
- 阶段 1：**通过**。
- 阶段 2：**通过自动化验收**。
- 下一阶段：阶段 3（PDF、DOCX、静态网页采集），尚未开始。

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
- Inbox、Knowledge、Jobs 页面；前端 Request ID、统一 API 错误、超时和 AbortController 取消。

## 当前验证结果

~~~text
uv sync --project backend --locked                    passed
uv lock --project backend --check                     passed
uv run --directory backend ruff check app tests       passed
uv run --directory backend pytest -q                  23 passed
temporary SQLite alembic up/down/up                    passed
npm --prefix frontend ci --ignore-scripts ...         passed
npm --prefix frontend run typecheck                   passed
npm --prefix frontend run test                        9 passed
npm --prefix frontend run build                       passed
FastAPI 127.0.0.1 runtime + GET /api/health            passed
Vite 127.0.0.1 runtime                                passed
~~~

测试全部使用临时 SQLite、Qdrant、Artifact 与 Vault；未使用真实个人 Vault 或真实 API Key。

## Git 与数据边界

- `.env`、SQLite、Qdrant、Artifact、Vault、`node_modules`、虚拟环境和构建缓存均被忽略。
- 当前仓库在本轮开始时为 `main` 且无 commit；本轮建立首次基线 commit，不 push。

## 真实剩余项

- environment limitation：本机没有 Docker，未本地执行 `docker build`；GitHub Actions 已提供对应 gate。
- environment limitation：当前浏览器 runner 曾报 `trusted Node process exited unexpectedly; kernel reset`；automated component verification passed，real browser E2E pending Playwright/browser-capable environment。
- future phase：FFmpeg 当前不可用，阶段 5 视频功能前再验证，不自行安装。
- future phase：PDF、DOCX、网页、Hybrid RAG、RAG Eval、视频、LangGraph、Agent、MCP Server/Client 仍按 `PROJECT.md` 后续阶段实施。
- blocking：无。
