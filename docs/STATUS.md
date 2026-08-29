# 项目状态

更新时间：2026-08-30

## 最终结论

- 阶段 0–3：已通过既定自动化和人工闭环验收。
- 阶段 4：**PASS WITH NON-BLOCKING RISKS**，已完成 Sol 独立复验。
- 阶段 5（批次 A–F）：**PASS WITH NON-BLOCKING RISKS**，已完成 Sol 最终复验。
- 阶段 6（批次 A–G）：**PASS WITH NON-BLOCKING RISKS**，已完成 Sol 独立总复验。
- 本轮最终极小收口仅加固 restore 归档/目标/staging 路径碰撞并统一文档；不新增产品功能、Graph 或 migration。

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
- 既有非阻塞风险：没有未经明确协议伪造的生产 reranker HTTP adapter；仅提供可注入 Protocol 和确定性参考实现。

## 阶段 5：视频

- 批次 A–F 已交付严格毫秒时间轴、0004 Artifact 生命周期与 pending/current 隔离、安全 URL/下载边界、字幕优先、受控 FFmpeg/ASR、条件视觉、视频 Citation、补偿发布和清理。
- 视频 Graph 接线继续复用 VideoService、Stage2Service、JobRunner、Artifact、Obsidian 和 IndexService；没有把视频结果变成第二正文主库。
- 阶段 5 三项既有风险继续有效：真实 yt-dlp/extractor/FFmpeg/网站和模型互操作未在本机验证；HTTPS 隧道不能观察明文 redirect 或精确重定向次数；跨 Vault、SQLite、Artifact、Qdrant 的进程级崩溃仍不具备物理事务，只能依赖补偿、校验和重建。

## 阶段 6：Graph、MCP 与恢复

- 批次 A 建立严格 Pydantic/TypedDict/Literal 契约、脱敏 state、独立 AsyncSqliteSaver 生命周期、HITL interrupt/resume 和失败恢复。
- 批次 B 的生产 IngestionGraph 使用系统 job UUID，薄适配既有 Stage2/Video/JobRunner；review/publish reject/cancel 会清除 pending 候选并持久化为可恢复的 failed 状态，已发布 current 不被删除。
- 批次 C 的 QA request 以脱敏 query hash、mode、limit、rewrite、source_types 形成 fingerprint；不同身份在检索/Provider 前以 `idempotency_conflict` fail closed，相同身份的完整执行受 mutation lock 保护并复用 ModelRun/Citation。对应真实业务 migration 为 0005。
- 批次 D/E 仅提供五个 MCP 工具和显式 profile 的受控 Client；输入 strict/`extra=forbid`，能力取交集，错误和结果递归脱敏，不启用 Agent 自动工具循环。Collection 关系使用最小 0006 migration。
- 批次 F 使用固定归档、manifest/hash、离线 restore 和显式 CLI。restore 默认不覆盖，业务/独立 checkpoint 主文件及 `-wal/-shm` sidecar 一并安装、清理、回滚；Qdrant 由已验证 Markdown/Artifact/SQLite 关系重建。
- 批次 G 已有合成端到端闭环、前端 Playwright 代码、E2E typecheck、CI 门禁和失败产物配置；本机浏览器 runner 的真实执行仍按风险登记。
- H1–H3 已落地实时健康探针与安全设置投影、人工合集/Markdown Frontmatter 收敛，以及受控 rescan/backup/rebuild；均未新增阶段外 Graph 或正文主来源。

## H4 最终极小收口

- `BackupRestoreService.restore_backup` 现在拒绝归档与业务/ checkpoint 主文件或任一 sidecar 重叠，拒绝归档位于 Artifact/Vault 目标内，并在创建实际 staging 后拒绝任何目标与 staging 的父子碰撞。
- 新增定向测试覆盖 archive==database、archive==checkpoint `-wal`、归档位于 Artifact/Vault、强制 staging 碰撞以及独立目标正常恢复。
- `frontend/tests/e2e/stage6.spec.ts` 使用合成 API route fixture 增加合集列表/详情/移除成员和设置页五类 Provider/FFmpeg/备份成功覆盖；未处理的 `/api/**` 请求直接失败，fixture 不含真实路径或 secret。
- `README.md`、本文件、`docs/ARCHITECTURE.md` 和 `docs/TESTING.md` 已同步本轮事实并保留 **PASS WITH NON-BLOCKING RISKS** 结论；本轮未修改 `AGENTS.md`、`docs/DECISIONS.md` 或 `docs/PROJECT.md`。

## 验证证据

~~~text
uv sync --project backend --locked                    passed
uv lock --project backend --check                     passed
uv run --directory backend ruff check app tests       passed
uv run --directory backend pytest -q tests/test_stage6_backup_restore.py tests/test_stage6_backup_cli.py
                                                        10 passed in 2.41s
Sol 全量 backend                                        209 passed
temporary SQLite alembic upgrade -> downgrade -> upgrade
                                                        passed（0001–0006）
npm --prefix frontend ci --ignore-scripts --no-audit --no-fund
                                                        passed；whatwg-encoding 弃用警告
npm --prefix frontend run typecheck                   passed
npm --prefix frontend run test                        5 files / 27 tests
npm --prefix frontend run build                       passed
npm --prefix frontend run e2e:typecheck               passed
npm --prefix frontend run e2e -- --list               passed：发现 3 个测试
npm --prefix frontend run e2e                         本批未运行：本机缺少 Playwright 1.53 Chromium
GitHub Actions commit ba7e247 / 4 H3 baseline jobs     passed
GitHub Actions commit 5995efb / run 33281127978        4 jobs passed；Playwright 实际执行 3 个合成 E2E
git -c safe.directory=D:/Work/zhiliutai -C D:/Work/zhiliutai diff --check
                                                        passed
~~~

后端全量、MCP、QA、Ingestion、restore/CLI、视频和迁移专项均使用临时数据或确定性 fake；没有访问真实 Vault、数据库卷、Artifact、网页、视频、模型、API key 或外部 MCP Server。

## 非阻塞风险与下一步

- 合集 rename 在 Vault 写入成功但 SQLite 提交前发生不可捕获进程崩溃时，watcher 可能收敛出新合集并留下旧空合集；不会丢失知识正文或成员关系。
- 提交 `5995efb0f39732b175994cdb7d450e8c2eccf144` 已推送；run `33281127978` 的 backend/frontend/docker/playwright 四个 jobs 均成功，Playwright 实际执行 3 个 H4 合成 E2E。本机仍未安装匹配版本的 Chromium，只登记为本机环境限制。
- 真实 extractor、yt-dlp、FFmpeg、ASR/Vision/OCR、模型、网页和外部 MCP 互操作未验证；Docker build 也只由 CI/具备 Docker 的环境承担。
- SQLite、Artifact、Vault、Qdrant 之间不具备跨存储物理事务；恢复后必须显式 rebuild，运行期依赖补偿和权威关系复核。
- 阶段 4 的 reranker 协议风险和阶段 5 的 HTTPS/真实视频互操作风险继续有效。
- 阶段 6 已完成收口；未宣布无风险，也未开始阶段 7。

## Git 与数据边界

- H4 代码/测试提交 `5995efb0f39732b175994cdb7d450e8c2eccf144` 已推送并通过上述四个 jobs；本次证据同步提交仍由 Sol 验收后执行。
- 本批未读取、未修改或纳入提交受保护的 `data/manual/`。
- `.env`、SQLite、Qdrant、Artifact、Vault、node_modules、虚拟环境和构建缓存均不纳入 Git。
