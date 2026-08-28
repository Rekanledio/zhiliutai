# 知流台项目协作约定

## 需求依据

- docs/PROJECT.md 是当前唯一的产品需求和架构基线。若其他文档与它冲突，以它为准，并在 docs/DECISIONS.md 记录需要修订的决定。
- 本项目按阶段实施：阶段 0–5 已通过；阶段 6 最终结论为 **PASS WITH NON-BLOCKING RISKS**（Sol 已完成最终复验）。除已批准的极小维护外，不扩展下一阶段功能。
- 参考界面只用于借鉴布局、信息层级、色彩氛围和交互节奏。不要复制其品牌、素材、原始文案或无关业务功能。

## 实施与验证

- 默认由 Luna 完成常规实现。只有 docs/PROJECT.md 第 14 节列出的复杂一致性、安全、RAG/LangGraph 或定向审查任务才委派 Sol。
- 先做小批次改动，再运行对应验证；未经明确要求不执行 git commit 或推送。
- 不安装系统软件，不修改项目目录外的文件。项目依赖只能按当前阶段需要放入仓库内的本地环境，并在状态文档中说明。
- 日常开发使用 SQLite、SQLite FTS5、Qdrant Local 和 Python JobRunner，不依赖 Docker、PostgreSQL、Redis 或 Celery。所有服务默认只绑定 127.0.0.1；Docker 只用于交付和 CI。
- 真实 Vault、用户数据、API Key、数据库卷、Artifact 和本机 .env 不进入 Git；只提交 .env.example。
- 测试使用临时 Vault 和脱敏 fixtures，不直接触碰用户 Vault。
- 阶段 4/6 RAG 与 Graph 测试使用 SQLite、SQLite FTS5、Qdrant Local、临时 Artifact、MockTransport
  和确定性 Provider；不得访问真实网页、真实 Vault 或真实模型密钥。

## 代码边界

- 用户确认后的知识正文唯一主来源是 Obsidian Markdown；数据库、Chunk 和 Embedding 是可重建派生数据或关系数据，不能偷偷形成第二份可编辑正文主库。
- AI 摘要、标签、合集和正文改写先进入待确认状态，不直接覆盖用户内容。
- URL、文件路径和模型密钥处理遵循 docs/PROJECT.md 的安全约束；密钥不得进入日志、前端、Markdown 或提交记录。
- 阶段 4 的 Qdrant payload 只用于候选过滤与校验；SQLite 的 published/current/非删除状态
  才是版本权威来源。证据不足时必须拒答，不得调用答案 Provider。

## 常用命令

~~~powershell
# Backend
uv sync --project backend
uv run --directory backend uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Frontend
npm --prefix frontend install
npm --prefix frontend run dev

# Tests
uv run --directory backend pytest
npm --prefix frontend run test
~~~

若 Docker 或 FFmpeg 不在本机可用，记录为环境限制，不自行下载安装。
