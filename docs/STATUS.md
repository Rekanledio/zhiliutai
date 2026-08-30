# 项目状态

更新时间：2026-08-31

## 当前收尾状态

- 阶段 0–6 的产品基线仍以 `docs/PROJECT.md` 为准；本轮批次 A–F 的实现、测试、文档、CI 和最终门禁已落地。
- 当前工作树的实现自测：后端全量连续两次均为 231 passed；前端 9 个测试文件、52 个测试通过；E2E typecheck 通过并发现 6 个场景。
- 本轮已完成 Sol 独立复验，结论为 **PASS WITH NON-BLOCKING RISKS**；不扩展阶段外范围。此前阶段验收结论保留为历史记录。
- 阶段 6 后的用户批准维护已接入可选本地 ASR、远程 Vision 与本地 Reranker；不新增第三个 Graph 或第二正文来源。

## 既有阶段验收记录

- 阶段 0–3：既有自动化和人工闭环验收记录保留。
- 阶段 4、阶段 5（批次 A–F）和阶段 6（批次 A–G）的既有 Sol 复验结论保留为历史记录；本轮收尾另经本次独立复验。

## 数据所有权与运行边界

- 用户确认后的知识正文唯一主来源仍是 Obsidian Markdown。
- SQLite 保存业务关系、任务、版本、引用、ModelRun、FTS5 和 workflow request 元数据；Artifact 保存原件及可重建产物；Qdrant 仅保存可重建向量，SQLite 的 published/current/非删除关系始终权威。
- 只有 `IngestionGraph` 与 `QuestionAnswerGraph` 两个 Graph。Graph 只编排普通 application service，不复制采集、发布、索引、RAG、Citation 或 Provider 规则。
- HITL 保留 `pending_review`/`pending_publish` 边界；证据不足时在调用答案 Provider 前拒答。
- 服务默认绑定 `127.0.0.1`。自动化只使用临时 SQLite、Qdrant Local、Artifact、Vault、MockTransport、确定性 provider 和受控 MCP memory transport。

## 阶段 4：RAG

- QueryProcessor、SQLite FTS5、Qdrant Local、RRF、证据门禁、CitationBuilder 和 `QuestionAnswerService` 已接入现有 API/SSE。
- Qdrant 结果必须回到 SQLite 复核 current、published、非删除和内容哈希；Citation 必须属于当前 ModelRun、ContentVersion、Chunk 和 KnowledgeItem。
- ModelRun/Citation 使用事务和 mutation lock 保护；固定中文评测每次创建全新的临时检索环境，不依赖随机 UUID 排序。
- 用户批准维护已选择 CPU 本地 `BAAI/bge-reranker-v2-m3`，通过现有可注入 Protocol 接入；失败仍保留 RRF 顺序，SQLite 状态复核和证据门禁不变。

## 阶段 5：视频

- 批次 A–F 已交付严格毫秒时间轴、0004 Artifact 生命周期与 pending/current 隔离、安全 URL/下载边界、字幕优先、受控 FFmpeg/ASR、条件视觉、视频 Citation、补偿发布和清理。
- 视频 Graph 接线继续复用 VideoService、Stage2Service、JobRunner、Artifact、Obsidian 和 IndexService；没有把视频结果变成第二正文主库。
- 阶段 5 的真实 yt-dlp/extractor/网站互操作仍未验证；FFmpeg 可用且本地 ASR、Reranker 和合成 PNG Vision 调用已验证。HTTPS 隧道不能观察明文 redirect 或精确重定向次数；跨 Vault、SQLite、Artifact、Qdrant 的进程级崩溃仍不具备物理事务，只能依赖补偿、校验和重建。

## 阶段 6：Graph、MCP 与恢复

- 批次 A 建立严格 Pydantic/TypedDict/Literal 契约、脱敏 state、独立 AsyncSqliteSaver 生命周期、HITL interrupt/resume 和失败恢复。
- 批次 B 的生产 IngestionGraph 使用系统 job UUID，薄适配既有 Stage2/Video/JobRunner；review/publish reject/cancel 会清除 pending 候选并持久化为可恢复的 failed 状态，已发布 current 不被删除。
- 批次 C 的 QA request 以脱敏 query hash、mode、limit、rewrite、source_types 形成 fingerprint；不同身份在检索/Provider 前以 `idempotency_conflict` fail closed，相同身份的完整执行受 mutation lock 保护并复用 ModelRun/Citation。对应真实业务 migration 为 0005。
- 批次 D/E 仅提供五个 MCP 工具和显式 profile 的受控 Client；输入 strict/`extra=forbid`，能力取交集，错误和结果递归脱敏，不启用 Agent 自动工具循环。Collection 关系使用 0006 migration；标签、标签关系与审核合集建议使用 0007 migration。
- 批次 F 使用固定归档、manifest/hash、离线 restore 和显式 CLI。restore 默认不覆盖，业务/独立 checkpoint 主文件及 `-wal/-shm` sidecar 一并安装、清理、回滚；Qdrant 由已验证 Markdown/Artifact/SQLite 关系重建。
- 批次 G 已有合成端到端闭环、前端 Playwright 代码、E2E typecheck、CI 门禁和失败产物配置；本轮 Playwright fixture 已扩展到 6 个场景，本机浏览器 runner 的真实执行仍按环境限制登记。
- H1–H3 已落地实时健康探针与安全设置投影、人工合集/Markdown Frontmatter 收敛，以及受控 rescan/backup/rebuild；本轮补齐收件箱四类入口、审核编辑、标签/合集建议、知识库筛选与冲突保护、首页真实数据和 JobAttempt 展示，均未新增阶段外 Graph 或正文主来源。

## 本轮产品收尾

- `BackupRestoreService.restore_backup` 现在拒绝归档与业务/ checkpoint 主文件或任一 sidecar 重叠，拒绝归档位于 Artifact/Vault 目标内，并在创建实际 staging 后拒绝任何目标与 staging 的父子碰撞。
- 新增定向测试覆盖 archive==database、archive==checkpoint `-wal`、归档位于 Artifact/Vault、强制 staging 碰撞以及独立目标正常恢复。
- `frontend/tests/e2e/stage6.spec.ts` 使用合成 API route fixture 覆盖文本/Markdown、MD/TXT/PDF/DOCX 队列、静态网页、视频、审核编辑与 approve/reject/cancel/publish、知识库编辑/冲突/reprocess/软删除、Dashboard/Jobs 恢复、证据拒答、合集和设置备份；未处理的 `/api/**` 请求直接失败，fixture 不含真实路径或 secret。
- 0007 migration 增加标签元数据、KnowledgeItemTag 关系和候选合集建议字段；建议只在审核发布边界后形成正式关系，Frontmatter 仍是确认后的 Markdown 表达。
- `README.md`、本文件、`docs/ARCHITECTURE.md` 和 `docs/TESTING.md` 已同步当前实现、测试数量和环境限制；未修改 `AGENTS.md` 或 `docs/PROJECT.md`。

## 可选生产 Provider 维护

- 本机按 Ryzen 7 5800H、16GB RAM、RTX 3060 Laptop 6GB 选择 `faster-whisper medium`：
  CUDA `int8_float16` 实机加载和合成音频推理通过，保留 CPU `int8` 安全回退。
- `deepseek-v4-flash-vision-exp` 只接收受控 FFmpeg 关键帧 bytes 的 Base64，禁止任意图片 URL、
  重定向和环境代理；合成 1×1 PNG 的真实 API 调用通过。
- CPU `BAAI/bge-reranker-v2-m3` 已用两条合成中文文本实际加载和重排，相关知识片段排序优先。
- 三项能力均懒加载；自动化仍使用 fake loader、MockTransport、合成 bytes 和临时 Artifact，
  不访问真实 Vault、视频、网页或模型。新增依赖与缓存均位于项目边界，模型缓存被 Git 忽略。

## 验证证据

~~~text
uv sync --project backend --locked                    passed
uv lock --project backend --check                     passed
uv run --directory backend ruff check app tests       passed
uv run --directory backend pytest -q tests/test_stage6_backup_restore.py tests/test_stage6_backup_cli.py
                                                        10 passed in 2.41s
当前工作树 backend pytest（连续两次全量）              231 passed each
temporary SQLite alembic upgrade -> downgrade -> upgrade
                                                        passed（0001–0007）
npm --prefix frontend ci --ignore-scripts --no-audit --no-fund
                                                        passed；whatwg-encoding 弃用警告
npm --prefix frontend run typecheck                   passed
npm --prefix frontend run test                        9 files / 52 tests
npm --prefix frontend run build                       passed
npm --prefix frontend run e2e:typecheck               passed
npm --prefix frontend run e2e -- --list               passed：发现 6 个测试
npm --prefix frontend run e2e                         6 failed：缺少 chromium_headless_shell-1178，均未启动浏览器
GitHub Actions commit ba7e247 / 4 H3 baseline jobs     passed
GitHub Actions 历史 H4 run 33281127978                 4 jobs passed；当时实际执行 3 个合成 E2E，不代表本轮改动
git -c safe.directory=D:/Work/zhiliutai -C D:/Work/zhiliutai diff --check
                                                        passed
~~~

后端全量、MCP、QA、Ingestion、restore/CLI、视频和迁移专项均使用临时数据或确定性 fake；没有访问真实 Vault、数据库卷、Artifact、网页、视频、模型、API key 或外部 MCP Server。

## 非阻塞风险与下一步

- 合集 rename 在 Vault 写入成功但 SQLite 提交前发生不可捕获进程崩溃时，watcher 可能收敛出新合集并留下旧空合集；不会丢失知识正文或成员关系。
- 历史提交 `5995efb0f39732b175994cdb7d450e8c2eccf144` 的 run `33281127978` 曾使 backend/frontend/docker/playwright 四个 jobs 成功，并执行 3 个 H4 合成 E2E；该结果不覆盖本轮未提交收尾改动。本机仍未安装匹配版本的 Chromium，只登记为本机环境限制。
- 真实 extractor、yt-dlp、真实用户视频/关键帧、OCR、网页和外部 MCP 互操作未验证；本轮只以合成输入验证 FFmpeg 可用、本地 ASR/Reranker 和 DeepSeek Vision 协议。Docker build 仍由 CI/具备 Docker 的环境承担。
- SQLite、Artifact、Vault、Qdrant 之间不具备跨存储物理事务；恢复后必须显式 rebuild，运行期依赖补偿和权威关系复核。
- 本地 Reranker 已有明确模型协议；其质量、CPU 首次加载耗时，以及阶段 5 的 HTTPS/真实视频互操作风险继续作为非阻塞风险。
- 阶段 6 已完成收口；未宣布无风险，也未开始阶段 7。

## Git 与数据边界

- 历史 H4 代码/测试提交 `5995efb0f39732b175994cdb7d450e8c2eccf144` 已推送并通过上述四个 jobs；当前收尾改动未提交，commit/push 仍待用户授权。
- 本批未读取、未修改或纳入提交受保护的 `data/manual/`。
- `.env`、SQLite、Qdrant、Artifact、Vault、node_modules、虚拟环境和构建缓存均不纳入 Git。
