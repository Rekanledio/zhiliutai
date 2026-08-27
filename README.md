# 知流台

知流台是单用户、本机优先的个人知识采集、整理、检索和问答工作台。用户确认后的正文唯一写入 Obsidian Markdown；SQLite 保存业务元数据、任务状态与 FTS5 索引；Qdrant Local 保存向量索引；来源与处理产物位于本地 Artifact 目录。

当前阶段：阶段 0、阶段 1 已通过；阶段 2 已实现 Text / Markdown → 草稿 → 审核 → Obsidian → Chunk/Embedding/Qdrant → Watcher 重索引闭环。PDF、DOCX、网页、RAG Chat 和视频仍按 `docs/PROJECT.md` 的后续阶段实施。

## 本地快速开始

~~~powershell
Copy-Item .env.example .env
uv sync --project backend --locked
uv run --directory backend alembic upgrade head
npm --prefix frontend ci --ignore-scripts --no-audit --no-fund

# 终端 1
uv run --directory backend uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 终端 2
npm --prefix frontend run dev
~~~

本地开发不需要 Docker、PostgreSQL、Redis 或独立 Qdrant Server。默认数据库为 `data/zhiliutai.db`，向量目录为 `data/qdrant/`，Artifact 目录为 `data/artifacts/`；均可用环境变量覆盖且被 Git 忽略。

`VAULT_PATH` 未配置时 API 和首页仍可运行，但发布/监听功能会明确显示未配置。Chat 与 Embedding 是独立 capability：Chat 默认使用 OpenAI-compatible API；Embedding 默认使用进程内 FastEmbed 与中文 `BAAI/bge-small-zh-v1.5`（512 维），模型缓存在被 Git 忽略的 `data/models/`。也可以切换到 OpenAI-compatible Embedding。Embedding 未配置时不允许伪装完成向量发布。

## 验证

~~~powershell
uv lock --project backend --check
uv run --directory backend ruff check app tests
uv run --directory backend pytest
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build
~~~

Dockerfile 是交付能力，Docker build 在 GitHub Actions 或具备 Docker 的环境验证，不是本地开发前置条件。真实浏览器 E2E 计划由 Playwright CI 执行；当前组件自动化已通过，但不能等同于浏览器 E2E 已通过。

## 安全边界

- `.env`、真实 Vault、SQLite、Qdrant、Artifact 和 API Key 不进入 Git。
- 自动化只使用临时 Vault、临时数据库和确定性假模型。
- 所有本机服务默认监听 `127.0.0.1`。
- 不绕过登录、Cookie、付费墙、DRM 或平台权限。

需求、架构、进度和测试分别见 `docs/PROJECT.md`、`docs/ARCHITECTURE.md`、`docs/STATUS.md`、`docs/TESTING.md`。
