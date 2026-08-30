# 知流台

知流台是单用户、本机优先的个人知识采集、整理、检索和问答工作台。用户确认后的正文唯一写入 Obsidian Markdown；SQLite 保存业务元数据、任务状态与 FTS5 索引；Qdrant Local 保存向量索引；来源与处理产物位于本地 Artifact 目录。

当前实现：阶段 0–6 的产品面与本轮批次 A–F 收尾已实现，包含两个 LangGraph（IngestionGraph、QuestionAnswerGraph）、HITL/checkpoint、五工具 MCP Provider、显式配置 MCP Consumer、人工标签/合集、只读设置、受控备份/重扫/派生重建，以及可用的收件箱、知识库、首页和任务页。用户确认后的正文仍只写入 Obsidian Markdown；真实模型、网页、视频和外部 MCP 不属于自动化测试。本轮已完成 Sol 独立复验，结论为 **PASS WITH NON-BLOCKING RISKS**；本机 Playwright 浏览器执行和真实外部互操作仍按非阻塞风险登记。

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

本机可选模型组合为：`faster-whisper` 的 `medium` ASR（`ASR_DEVICE=auto`，优先 CUDA `int8_float16`、失败退回 CPU `int8`）、DeepSeek `deepseek-v4-flash-vision-exp` Vision，以及 CPU 运行的 `BAAI/bge-reranker-v2-m3`。本地模型均懒加载并缓存在 `data/models/`；应用启动和健康探针不会下载模型。Vision 只发送 FFmpeg 在受管 Artifact 临时目录中生成的有界关键帧 Base64，不接受任意图片 URL 或本地路径。真实凭据只写入项目根目录 `.env`，其中 `VISION_API_KEY` 必须显式配置，不能由前端提交。

合集页管理 SQLite 中的合集关系，并把人工成员变更同步到受管理 Markdown 的 `collections` Frontmatter；重扫/监听可按 Markdown 主来源收敛关系，删除合集不删除知识正文、Artifact 或向量。设置页只显示严格脱敏的受管理相对目录、五类 Provider 配置状态、RAG/切分参数、视频保留和 FFmpeg 状态；配置仍通过项目根目录 `.env`，页面不接收或显示 API Key、base URL 或绝对路径。FFmpeg 探针与首页使用同一个 `VIDEO_FFMPEG_EXECUTABLE` 配置，未配置只表示视频能力降级。

## 离线恢复演练与 MCP

恢复只能在 FastAPI、JobRunner、Obsidian watcher 和 workflow checkpoint 使用者全部停止后，由短生命周期 CLI 执行；CLI 不从归档推导目标，也默认拒绝覆盖。以下命令只使用临时目录变量，运行前按实际环境替换路径：

默认备份根目录为 `data/backups/`，该目录已被 Git 忽略。归档路径必须独立于业务/ checkpoint SQLite 主文件及其 `-wal/-shm` sidecar，也不能位于 Artifact 或受管理 Vault 恢复目标内；restore 创建 staging 后还会拒绝与恢复目标发生父子路径碰撞。

~~~powershell
$runRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhiliutai-" + [guid]::NewGuid().ToString("N"))
$archive = Join-Path $runRoot "backup.zip"
$database = Join-Path $runRoot "business.sqlite"
$checkpoint = Join-Path $runRoot "checkpoint.sqlite"
$artifacts = Join-Path $runRoot "artifacts"
$managedVault = Join-Path $runRoot "vault-managed"
$qdrant = Join-Path $runRoot "qdrant"
$restoreRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhiliutai-restore-" + [guid]::NewGuid().ToString("N"))
$restoredDatabase = Join-Path $restoreRoot "business.sqlite"
$restoredCheckpoint = Join-Path $restoreRoot "checkpoint.sqlite"
$restoredArtifacts = Join-Path $restoreRoot "artifacts"
$restoredVault = Join-Path $restoreRoot "vault-managed"
$restoredQdrant = Join-Path $restoreRoot "qdrant"

# 先完成 migration，并确认这些变量指向临时运行根中的待备份数据：
uv run --directory backend python ../scripts/zhiliutai_backup.py backup `
  --archive $archive --database $database --checkpoint $checkpoint `
  --artifacts $artifacts --managed-vault-root $managedVault --qdrant $qdrant

# 停止 API、JobRunner、watcher 和其他 checkpoint 使用者后再恢复：
uv run --directory backend python ../scripts/zhiliutai_backup.py restore `
  --archive $archive --database $restoredDatabase --checkpoint $restoredCheckpoint `
  --artifacts $restoredArtifacts --managed-vault-root $restoredVault --qdrant $restoredQdrant

# 按配置提供受控 Embedding capability 后，从 Markdown/Artifact/SQLite 重建派生索引：
uv run --directory backend python ../scripts/zhiliutai_backup.py rebuild `
  --database $restoredDatabase --checkpoint $restoredCheckpoint --artifacts $restoredArtifacts `
  --managed-vault-root $restoredVault --qdrant $restoredQdrant
~~~

MCP Provider 的受控 stdio 入口是 `uv run --directory backend python -m app.mcp.server`；stdout 只承载协议帧，日志不作为工具响应。MCP Consumer 使用 `app.mcp.client` 的显式 `MCPServerProfile`/可信 entrypoint 注册表，只连接明确配置的 server，不接受模型提供的 command、路径、endpoint 或任意 shell，也不启用自动工具循环。

## 验证

~~~powershell
uv lock --project backend --check
uv run --directory backend ruff check app tests
uv run --directory backend pytest
uv run --directory backend pytest
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build
npm --prefix frontend run e2e:typecheck
npm --prefix frontend run e2e -- --list
npm --prefix frontend run e2e
~~~

Dockerfile 是交付能力，Docker build 在 GitHub Actions 或具备 Docker 的环境验证，不是本地开发前置条件。Playwright 使用固定版本、127.0.0.1 Vite 服务和稳定合成 API fixture；CI 安装 Chromium 并保存失败产物。本机 runner 的实际限制不会被伪装成通过。

当前工作树本地维护验证：后端全量连续两次均为 231 passed；前端为 9 files / 52 tests，typecheck、build 和 E2E typecheck 通过，Playwright 清单发现 6 个测试。真实 Playwright 执行因本机缺少 `chromium_headless_shell-1178` 未能启动，未记为通过；既有 CI 的 3 个 E2E 证据只代表当时提交，不替代本轮 6 个场景的浏览器执行。

## 安全边界

- `.env`、真实 Vault、SQLite、Qdrant、Artifact 和 API Key 不进入 Git。
- 自动化只使用临时 Vault、临时数据库和确定性假模型。
- 所有本机服务默认监听 `127.0.0.1`。
- 不绕过登录、Cookie、付费墙、DRM 或平台权限。
- Graph state/checkpoint 只保存脱敏的稳定标识和路由结果；MCP Client 只连接显式配置的受信入口，不启用自动工具循环。
- 备份归档显式区分 SQLite/Artifact/Vault 与可重建的 Qdrant；恢复默认拒绝覆盖，重建前验证 Markdown、Artifact 和 SQLite 权威关系。

需求、架构、进度和测试分别见 `docs/PROJECT.md`、`docs/ARCHITECTURE.md`、`docs/STATUS.md`、`docs/TESTING.md`。
