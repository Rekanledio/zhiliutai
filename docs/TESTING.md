# 测试与验证策略

## 本地门禁

~~~powershell
uv sync --project backend --locked
uv lock --project backend --check
uv run --directory backend ruff check app tests
uv run --directory backend pytest

npm --prefix frontend ci --ignore-scripts --no-audit --no-fund
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build
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

阶段 2 人工闭环使用专用测试 Vault，验证真实 Chat provider、FastEmbed、审核发布、Obsidian 打开与连续多次外部修改。人工浏览器验收与 Playwright 自动化是两个不同结论：前者已完成，后者仍等待 CI/browser-capable environment。

阶段 3 使用 backend/tests/fixture_sources.py 确定性生成中文 PDF 和 DOCX
bytes，并使用内存中的静态 HTML 与 httpx.MockTransport，覆盖 PDF 页码、
DOCX 标题层级/表格行、网页标题层级/最终 URL、网页快照 Artifact、统一草稿/
审核/发布/Chunk locator，以及 URL 私网地址和重定向目的地复验。测试不访问
真实网页，不读取 Cookie 或个人文件，也不提交二进制 fixture。

前端覆盖 Dashboard、最终 Health 组件、七项导航、Inbox 提交、Job 状态、审核、发布、Obsidian watcher 状态、统一错误与后端 Request ID、请求超时、AbortController 取消和离线降级。

## 环境能力门禁

- Docker build：GitHub Actions 或具备 Docker 的环境；不要求本机安装 Docker。
- 真实浏览器自动化：Playwright CI 或浏览器能力正常的环境。当前 automated component verification passed，人工浏览器闭环 passed；Playwright E2E pending。
- FFmpeg：阶段 5 视频验收再验证；当前未安装且不自行安装。
- 阶段 3 URL 获取：只测试公网 DNS 解析和 mock transport；不使用真实个人网页凭据。

## 数据安全

- 自动化只能使用测试创建的临时 Vault、临时 SQLite、临时 Qdrant 和临时 Artifact。
- 不读取、修改或清理真实个人 Vault。
- `.env`、API Key、真实用户数据和生成数据库不得进入 Git。
