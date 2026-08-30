# 测试与验证策略

当前文档描述阶段 0–6 的实际实现和本轮收尾门禁；本轮已完成 Sol 独立复验，结论为 **PASS WITH NON-BLOCKING RISKS**。既有 CI 记录只作为历史证据保留。本机 Playwright 的真实启动失败按环境限制登记，不把测试收集或清单结果写成浏览器通过。

## 本地门禁

~~~powershell
uv sync --project backend --locked
uv lock --project backend --check
uv run --directory backend ruff check app tests
uv run --directory backend pytest
uv run --directory backend pytest

npm --prefix frontend ci --ignore-scripts --no-audit --no-fund
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build
npm --prefix frontend run e2e:typecheck
npm --prefix frontend run e2e -- --list
~~~

SQLite migration 必须对临时或明确的开发数据库运行，不触碰真实数据：

~~~powershell
$migrationDb = Join-Path ([IO.Path]::GetTempPath()) ("zhiliutai-" + [guid]::NewGuid().ToString("N") + ".db")
$env:DATABASE_URL = "sqlite+aiosqlite:///" + ($migrationDb -replace "\\","/")
uv run --directory backend alembic upgrade head
uv run --directory backend alembic downgrade base
uv run --directory backend alembic upgrade head
~~~

运行态：

~~~powershell
uv run --directory backend uvicorn app.main:app --host 127.0.0.1 --port 8000
npm --prefix frontend run dev
~~~

验证 `GET /api/health`，但 HTTP 200 不替代组件或浏览器行为测试。

## 当前自动化覆盖

后端覆盖 SQLite 健康/失败、WAL、Qdrant Local 健康与持久检索、Artifact 读写、Vault 未配置、Vault 配置但 watcher 未运行、模型未配置/不可达、配置路径解析、404/422/500 Request ID、真实 migration 升降级、JobRunner 失败/重试/重启恢复。

阶段 2 使用临时 Vault 和确定性 provider，覆盖 Text/Markdown 采集、规范化哈希、去重与幂等冲突、草稿/审核/发布、稳定 Frontmatter、Vault 原子写、Chunk、FTS5/Qdrant、外部修改 rescan、watcher 增量重索引、连续快速保存防抖、瞬态无效 Markdown 回退、当前版本索引收敛、网页编辑冲突和软删除不删除 Markdown。

阶段 2 人工闭环使用专用测试 Vault，验证真实 Chat provider、FastEmbed、审核发布、Obsidian 打开与连续多次外部修改。人工浏览器验收与 Playwright 自动化是两个不同结论：前者已完成，后者由 CI Chromium 负责；本机仍受 browser-capable environment 限制。

阶段 3 使用 backend/tests/fixture_sources.py 确定性生成中文 PDF 和 DOCX
bytes，并使用内存中的静态 HTML 与 httpx.MockTransport，覆盖 PDF 页码、
DOCX 标题层级/表格行、网页标题层级/最终 URL、网页快照 Artifact、统一草稿/
审核/发布/Chunk locator，以及 URL 私网地址和重定向目的地复验。测试不访问
真实网页，不读取 Cookie 或个人文件，也不提交二进制 fixture。

阶段 4 使用临时 SQLite、Qdrant Local、临时 Artifact、确定性 RAG Provider 和
合成 fixtures，覆盖 QueryProcessor、FTS5/Qdrant hybrid、RRF 去重、current
version SQLite authority、软删除/未发布排除、EvidencePolicy、CitationBuilder
的 PDF/DOCX/网页/Obsidian locator、安全 Artifact target、RagChatProvider、
claim/citation 校验、ModelRun/Citation 审计、知识变化复核、SSE 事件、reranker
失败降级和固定中文离线评测。阻塞修复还覆盖 Qdrant payload 与 SQLite 不一致、
伪造/失效/跨回答 citation、Windows 绝对路径和路径穿越、验证错误与 ModelRun/
Citation/SSE 的敏感信息脱敏、版本切换重试、Artifact HEAD/PDF 页码 target、
Obsidian 不可访问 target，以及 SSE 顺序。固定评测真实调用 HybridRetriever，
固定评测每次运行都重新创建临时 SQLite/FTS5、Qdrant Local、Artifact、Vault 和确定性
fixtures，并验证 FTS/RRF/向量并列排序不依赖随机 UUID；只报告观测指标，不设置
PROJECT.md 未定义的硬阈值。网页相关测试只使用 httpx.MockTransport。

阶段 5 批次 A 使用纯合成输入覆盖视频契约：严格非负整数毫秒、非空时间段、已知
`duration_ms` 的嵌套边界、视频 URL 凭据拒绝，以及离线确定性 ASR/Vision/OCR fake 的重复性。
0004 migration 在临时 SQLite（显式 `PRAGMA foreign_keys=ON`）上覆盖结构化 Artifact
metadata、保留/清理默认值、`until_expiry` 到期时间一致性、清理到期索引、同条目
pending/current 版本隔离、跨条目 pending INSERT/UPDATE 拒绝、pending 删除后的
`SET NULL` 和 upgrade/downgrade/upgrade 数据回环。Health key 测试只使用合成 key 与
mock HTTP client；不访问真实视频、模型、网页、Vault、Artifact 或 API key。

阶段 6 批次 A 使用临时 checkpoint SQLite 和确定性 fake，覆盖两个 Graph 的编译/运行、
Pydantic `extra=forbid`、非法 ID/route/decision、review/publish interrupt/resume、
拒绝/取消、checkpoint 关闭重开后恢复、失败父 checkpoint 重试、重复 resume 幂等、
证据不足拒答且 answer 零调用，以及 API key、Authorization、Cookie、Windows Vault
绝对路径和 traceback sentinel 不落盘。测试不接入生产 API、JobRunner、Stage2/Video、
RAG service、真实 Vault、网页、模型或外部 MCP。

阶段 6 批次 B 使用临时 SQLite、临时 Qdrant/Artifact/Vault 和确定性 video/draft provider，
覆盖生产 JobRunner handler 进入 IngestionGraph、job UUID 到 checkpoint thread 的稳定映射、
文本/Markdown/PDF/DOCX/网页/视频 source route、review interrupt → resume → publish
interrupt → resume、拒绝/取消、失败后从父 checkpoint 重试、应用级 checkpoint 生命周期、
重复 publish 不产生新 ContentVersion、视频字幕/ASR/视觉现有降级契约，以及 JobRunner 错误
摘要的截断和脱敏。Graph 仍不保存 URL、路径、正文、媒体或 provider 响应；测试不访问真实
Vault、网页、视频、模型、API key 或外部 MCP。

阶段 6 批次 C 使用临时 SQLite、临时 checkpoint 和确定性 RAG provider，覆盖生产
QuestionAnswerGraph 对现有 `QuestionAnswerService.answer()` 原子边界的编排、原有 SSE 事件顺序、
sufficient/refuse 路由、拒答零 Provider、canonical request UUID、重复 request 幂等、ModelRun/Citation
唯一归属、checkpoint 未推进后的新 runtime 恢复、当前 published/current/非删除重验证、失败脱敏和
0005 migration 往返。测试不保存 Chunk/证据全文或 Provider 原始响应到 state/checkpoint，也不访问
真实 Vault、网页、模型、密钥或外部 MCP。

阶段 6 批次 D 使用锁定的 `mcp==2.1.1`、临时 SQLite、受控 fake 和 SDK 内存 transport，覆盖五个
工具的精确注册与 strict `extra=forbid` schema、共享 application service 调用、Collection 查询、
非法额外字段/UUID/Windows 路径、loopback SSRF fail-closed、稳定错误脱敏和响应边界；0005/0006
migration 在临时 SQLite 上执行 upgrade/downgrade/upgrade。当前 0007 migration 还增加 Tag、
KnowledgeItemTag 和候选合集建议字段，并由 Stage2/Frontmatter 测试覆盖往返和收敛。MCP Server 的 stdio 入口不输出协议外
日志；测试不连接真实外部 MCP Server，也不访问真实 Vault、网页、视频、模型或密钥。

阶段 6 批次 E 使用实际 `mcp==2.1.1` ClientSession、stdio 生命周期实现和进程内 memory transport，
覆盖结构化 server profile、额外 command/endpoint/transport 拒绝、initialize 身份校验、tools/list
能力交集、恶意/开放 JSON Schema、参数注入、结果注入与 secret/traceback/路径脱敏、响应大小、
超时、取消、断线、远端异常和 context close。测试只由代码注入受控 MCP Server，不配置或连接真实
外部 server，也不启用 Agent 自动工具循环。

阶段 6 批次 F 使用临时 SQLite、独立 checkpoint SQLite、临时 Artifact/Vault/Qdrant，覆盖 SQLite
一致性 backup、checkpoint 归档、manifest/hash、归档损坏/穿越/符号链接/schema 与既有目标拒绝、
显式覆盖、归档与 SQLite/Artifact/Vault/staging 路径碰撞、安装中断回滚、删除运行数据后的 restore、从 Obsidian/Artifact/SQLite 权威重建
Chunk/FTS5/Qdrant、查询结果一致，以及无 Vault/模型 secret 的空环境 degraded 启动。测试只使用
合成 Markdown 和确定性 embedding，不访问真实 Vault、网页、视频、模型、密钥或外部 MCP。

阶段 6 批次 F 定向命令：

~~~powershell
uv run --directory backend ruff check app/services/backup.py app/services/vector_store.py tests/test_stage6_backup_restore.py tests/test_stage6_backup_cli.py
uv run --directory backend pytest -q tests/test_stage6_backup_restore.py tests/test_stage6_backup_cli.py
~~~

## 本轮 P1 阻塞修复回归

修复后的定向回归必须覆盖同一 QA request ID 的 query/mode/options fingerprint、并发 claim/execute/finish、
review/publish reject/cancel 的 item/job/pending 状态、MCP 正常正文与 Cookie/Authorization/绝对路径脱敏，
以及 restore 离线门禁、SQLite `-wal/-shm` sidecar 覆盖/回滚和显式 CLI：

~~~powershell
uv run --directory backend pytest -q tests/test_stage6_question_answer_production.py tests/test_rag_api.py
uv run --directory backend pytest -q tests/test_stage6_ingestion_production.py tests/test_stage6_graph_checkpoint.py tests/test_stage6_graph_contracts.py
uv run --directory backend pytest -q tests/test_stage6_mcp_server.py tests/test_stage6_mcp_client.py
uv run --directory backend pytest -q tests/test_stage6_backup_restore.py tests/test_stage6_backup_cli.py
~~~

备份/恢复演练只对临时目录执行。先在空环境完成 migration `upgrade head → downgrade base → upgrade head`，
再运行 `scripts/zhiliutai_backup.py backup`。执行 restore 前必须停止 FastAPI、JobRunner、Obsidian watcher
和所有 workflow checkpoint 使用者；restore 命令必须显式给出 archive、database、checkpoint、artifacts、
managed-vault-root、qdrant 目标，默认不覆盖。恢复后运行 `rebuild`，再用 `/api/search` 或等价的临时
application service 查询结果并核对 SQLite `published/current/非删除` 权威关系。归档目标不从 manifest 推导，
不得把真实 Vault、数据库卷、Artifact、API key 或外部 MCP 带入演练。

示例入口（将变量指向同一临时运行根，不填写真实路径）：

~~~powershell
uv run --directory backend python ../scripts/zhiliutai_backup.py backup --archive $archive --database $database --checkpoint $checkpoint --artifacts $artifacts --managed-vault-root $managedVault --qdrant $qdrant
# 停止所有运行时后：
uv run --directory backend python ../scripts/zhiliutai_backup.py restore --archive $archive --database $database --checkpoint $checkpoint --artifacts $artifacts --managed-vault-root $managedVault --qdrant $qdrant
uv run --directory backend python ../scripts/zhiliutai_backup.py rebuild --database $database --checkpoint $checkpoint --artifacts $artifacts --managed-vault-root $managedVault --qdrant $qdrant
~~~

MCP Provider 的实际入口为 `uv run --directory backend python -m app.mcp.server`，使用 stdio 时 stdout 只
输出协议帧；Consumer 必须由应用代码显式选择 `MCPServerProfile` 和受信 `TrustedStdioEntrypoint`，测试中
仅注入进程内 memory transport。测试不得启用 Agent 自动工具循环、接受模型 command/endpoint/path，或连接
用户配置之外的 server。

阶段 6 批次 G 使用临时 SQLite、Qdrant、Artifact、Vault、确定性 provider 和受控 MCP memory
transport，覆盖采集 → JobRunner/Graph 处理 → 人工审核 → 发布到 Obsidian → SQLite/Qdrant
检索 → 带 Citation 的 SSE 回答 → MCP `search_knowledge`/`get_item` 查询闭环。前端 Playwright
使用固定 `@playwright/test==1.53.0`、`workers=1`、127.0.0.1 Vite webServer 和浏览器内合成
API fixture，覆盖文本/Markdown、MD/TXT/PDF/DOCX 文件队列、静态网页、视频、收件箱审核编辑与
approve/reject/cancel/publish、知识库筛选/编辑冲突/reprocess/软删除、Dashboard/Jobs 恢复、合集、
设置页五类 Provider、FFmpeg 未配置说明、一次合成备份、搜索、证据拒答、证据回答和 Citation UI；所有未处理的 `/api/**` 请求都会使
测试失败。失败时保留 trace、screenshot 和 video。CI 还执行独立 E2E typecheck、Chromium 安装
和 Playwright，并上传 `playwright-report` 与 `test-results`。当前清单应发现 6 个测试；历史提交 `ba7e247aac0213c85e223bff3238143af16a99f8`
的四个 H3 基线 jobs 均通过；H4 提交 `5995efb0f39732b175994cdb7d450e8c2eccf144` 的 run
`33281127978` 已在 CI Chromium 执行 3 个合成 E2E 并通过。本机未安装 Playwright 1.53 对应
Chromium，因此本轮本机真实 E2E 执行 6 条均在浏览器启动前失败，仍作为环境限制。既有阶段 6 结论只保留为历史记录。
测试不访问真实 Vault、网页、视频、模型、密钥或外部 MCP。

阶段 6 批次 G 定向命令：

~~~powershell
uv run --directory backend ruff check tests/test_stage6_end_to_end.py
uv run --directory backend pytest -q tests/test_stage6_end_to_end.py
npm --prefix frontend run e2e:typecheck
npm --prefix frontend run e2e -- --list
npm --prefix frontend run e2e
~~~

本轮合成浏览器收口的本机结果：`e2e:typecheck` 通过，`e2e -- --list` 发现 6 个测试；
`npm --prefix frontend run e2e` 的 6 个测试均因缺少
`C:\Users\Lenovo\AppData\Local\ms-playwright\chromium_headless_shell-1178\chrome-win\headless_shell.exe`
在浏览器启动前失败。不下载或安装浏览器。历史 H4 run 只执行过当时的 3 个场景，不能替代本轮 6 个场景。

## 阶段 6 H1–H3 维护功能

- H1 定向测试使用 MockTransport 和合成配置，覆盖远程 Provider 的 2xx、认证失败、其他
  4xx/5xx、超时、连接失败、loopback 无 key、FastEmbed 与远程能力聚合，以及首页/设置页共用
  `VIDEO_FFMPEG_EXECUTABLE` 的 FFmpeg 探针；响应不包含 key、Authorization/Cookie、查询串、
  绝对路径或 traceback。
- H2 定向测试使用临时 SQLite/Vault，覆盖合集 CRUD、已发布 current 成员过滤、关系幂等、
  `collections` Frontmatter 写入与 rescan 收敛；用户修改的 tags 和正文哈希保持不变，删除合集
  不删除知识正文、Artifact 或向量。前端测试覆盖合集列表/详情/成员移除和空态。
- H3 定向测试使用临时运行根，覆盖严格设置响应、五类 Provider、维护操作的确认/锁/错误边界、
  服务端生成备份归档，以及 rescan/rebuild 复用普通 application service。客户端不提交路径、
  文件名、overwrite 或密钥；restore 只由离线 CLI 执行，`BACKUP_ROOT=data/backups/` 已被 Git
  忽略。

阶段 5 批次 B 使用 `httpx.MockTransport`、确定性 DNS 和临时 Artifact/SQLite，覆盖视频
入口字段白名单、幂等重复提交、IPv4/IPv6/链路本地/私网/元数据/文档地址、每个显式重定向、
DNS rebinding 风格输入、凭据 URL、敏感参数、非法 scheme、本地路径、大小/超时、错误脱敏
和旧 current/published 不变。`YtDlpDownloader` 覆盖默认 loopback 安全执行器及注入的离线
执行器，并验证 `--ignore-config`、`--no-plugin-dirs`、固定 loopback `--proxy`、
`--downloader native`、无 Cookie/exec/任意配置参数、无 ambient proxy 环境和受控目录。
安全代理测试只连接本机临时 listener：合成公网 DNS 结果由注入 connector 记录 numeric IP 后
失败，下一次私网/loopback 结果在 connector 前被拒绝，以证明每次目标解析、全地址拒绝和
connect-to-validated-IP；不运行真实 yt-dlp，也不访问真实网页。

阶段 5 批次 C 覆盖字幕优先（字幕存在时 ASR 零调用）、无字幕 `asr_required`、受控 FFmpeg
固定参数、FakeAudio/ASR 兜底、工具/解析超时和限额、取消、失败清理、重试与失败恢复。
阶段 5 批次 D 覆盖仅幻灯片/教程触发视觉、访谈跳过、关键帧内容哈希去重、OCR/视觉文本
安全、时间线对齐和确定性 fake；不调用真实模型。

阶段 5 批次 E 覆盖字幕越界/乱序/恶意内容、manifest 的毫秒时序验证、pending/current
隔离、只在审核发布后写 Vault 和重建 Chunk/FTS5/Qdrant、视频 transcript/chapter/keyframe
Citation 的 exact/fallback、Artifact 路径/哈希/保留策略和跨版本复核；同时使用本机 locator
API 覆盖字幕时间戳、keyframe、缺失/拒绝 Artifact 和浏览器阻止图片。发布故障注入覆盖
暂存 Vault、Embedding 失败、Qdrant 部分写入后失败、Vault 交换失败、候选向量清理和重试，
断言旧 Vault/current/旧索引仍可用；Qdrant 不作为版本权威。阶段 5 批次 F 覆盖 Inbox/Jobs/
Search 最小闭环、取消、reprocess 和媒体清理。

阶段 5 定向测试命令按批次先运行，再执行全量门禁：

~~~powershell
uv run --directory backend pytest -q tests/test_stage6_graph_contracts.py tests/test_stage6_graph_checkpoint.py
uv run --directory backend pytest -q tests/test_stage6_ingestion_production.py
uv run --directory backend pytest -q tests/test_stage6_graph_contracts.py tests/test_stage6_graph_checkpoint.py tests/test_stage6_ingestion_production.py tests/test_stage2_flow.py tests/test_source_pipeline.py tests/test_video_pipeline.py tests/test_video_stage5.py
uv run --directory backend pytest -q tests/test_stage6_question_answer_production.py tests/test_rag_api.py tests/test_rag_answer.py tests/test_rag_retrieval.py tests/test_rag_security.py tests/test_migrations_and_vector.py
uv run --directory backend pytest -q tests/test_stage6_mcp_server.py tests/test_migrations_and_vector.py::test_migration_upgrade_downgrade_upgrade tests/test_migrations_and_vector.py::test_video_lifecycle_migration_defaults_constraints_and_round_trip
uv run --directory backend pytest -q tests/test_stage6_mcp_client.py
uv run --directory backend pytest -q tests/test_video_contracts.py
uv run --directory backend pytest -q tests/test_video_pipeline.py tests/test_video_stage5.py
uv run --directory backend pytest -q tests/test_source_pipeline.py tests/test_rag_api.py tests/test_rag_answer.py tests/test_rag_retrieval.py
npm --prefix frontend run typecheck
npm --prefix frontend run test
~~~

前端覆盖 Dashboard、最终 Health 组件、七项导航、Inbox 提交、Job 状态、审核、发布、Obsidian watcher 状态、搜索结果、SSE 问答与 citation 卡片、统一错误与后端 Request ID、请求超时、AbortController 取消和离线降级。

## 环境能力门禁

- Docker build：GitHub Actions 或具备 Docker 的环境；不要求本机安装 Docker。
- 真实浏览器自动化：当前工作树有 6 个 Playwright 合成场景；本机因缺少 Playwright 1.53 对应 Chromium 无法启动，不能标记为通过。历史 H4 CI run `33281127978` 实际执行过当时的 3 个场景；本轮新改动须由 CI 或具备浏览器能力的环境重新执行。
- FFmpeg/yt-dlp：FFmpeg 命令在本机可用；自动化仍使用注入 runner 验证固定参数和路径边界。
  真实 yt-dlp/extractor/网站仍未调用；缺失二进制必须返回 capability/degraded，不得伪装互操作通过。
- 阶段 3 URL 获取：只测试公网 DNS 解析和 mock transport；不使用真实个人网页凭据。
- 可选生产 Provider 维护：`tests/test_production_capabilities.py` 使用 injected model loader、
  `MockTransport`、合成图片/音频和临时 Artifact，覆盖 faster-whisper CUDA 初始化失败退回 CPU、
  Vision 仅发送 Base64 且拒绝未知图片格式、FFmpeg sampler 路径越界、BGE 分数映射与
  `create_app` 懒加载装配。自动化不得下载或加载真实模型。
- 2026-08-30 本机人工维护验证另行使用公开缓存与合成输入：`faster-whisper medium` 在 RTX 3060
  CUDA `int8_float16` 运行通过，CPU `BAAI/bge-reranker-v2-m3` 重排通过，DeepSeek Vision 合成
  PNG 请求通过。该证据不代表真实用户视频、画面质量、费用或长期供应商可用性验证。

## 数据安全

- 自动化只能使用测试创建的临时 Vault、临时 SQLite、临时 Qdrant 和临时 Artifact。
- 不读取、修改或清理真实个人 Vault。
- `.env`、API Key、真实用户数据和生成数据库不得进入 Git。
