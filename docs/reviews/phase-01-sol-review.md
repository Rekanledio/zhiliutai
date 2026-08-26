# 阶段 0/1 Sol 审查报告

审查日期：2026-08-26
审查模型：gpt-5.6-sol
需求基线：`docs/PROJECT.md`

## 1. 结论

| 阶段 | 结论 | 说明 |
| --- | --- | --- |
| 阶段 0：项目基线 | **有条件通过** | Git、目录、环境示例、Python/npm 锁文件和主要忽略规则已落地，锁定安装可复现；但生成的 `frontend/tsconfig.app.tsbuildinfo` 未忽略、当前所有项目文件仍未跟踪，且 README 与实际阶段状态有漂移。进入阶段 2 前应先完成基线清理。 |
| 阶段 1：可运行骨架和首页 | **不通过** | API 与前端可以在 PostgreSQL/Redis 缺失时启动，已有测试、静态构建和离线迁移均通过；但健康状态存在可复现误报，500 错误缺少响应头请求 ID，pgvector 和在线基础设施未验收，前端测试没有覆盖 React 行为，本轮也未能独立完成浏览器复验。 |

因此，**当前不满足直接进入阶段 2 的门槛**。应先由 Luna 完成 `docs/PHASE-2-PLAN.md` 的批次 0，并在具备 PostgreSQL/pgvector、Redis 和可用浏览器测试条件的环境中复验阶段 1。

本轮未发现 P0。未发现真实 `.env`、用户 Vault、Artifact、数据库卷或 API Key 被纳入仓库的证据。

## 2. 审查范围与方法

已按顺序阅读 `AGENTS.md`、`docs/PROJECT.md`、`docs/ARCHITECTURE.md`、`docs/DECISIONS.md`、`docs/STATUS.md` 和 `docs/TESTING.md`。

随后核对了项目结构、Git 状态、未跟踪文件、依赖和锁文件、Compose、环境示例、FastAPI、React、迁移、健康探针、日志、错误处理、首页代码和测试。实际执行了锁定依赖恢复、测试、静态检查、构建、离线迁移、API 启动和 HTTP 请求。

验收依据是 `docs/PROJECT.md:370-392`。尤其是阶段 1 要求“从空环境启动后，浏览器可访问首页，所有基础服务状态正确”（`docs/PROJECT.md:384`），不能只以 `docs/STATUS.md` 的自报结论代替。

## 3. 发现项

### P0

无。

### P1：必须在阶段 2 前修复

#### P1-01 健康接口会把未实际工作的组件报告为 `healthy`

- `backend/app/services/health.py:98-105` 只检查配置路径是否为目录，就把“Obsidian 监听器”标为健康，并返回“监听器骨架已就绪”；仓库中 `backend/app/obsidian/` 只有占位文件，没有监听器进程或心跳。
- `backend/app/services/health.py:108-115` 只校验 Chat URL 语法和模型名，不连接服务；不可达的 `http://127.0.0.1:9/v1` 仍返回 `state='healthy'`。
- `backend/app/services/health.py:40-65` 对 PostgreSQL 只执行 `SELECT 1`；`backend/app/db/migrations/versions/0001_baseline.py:11-17` 是空迁移，没有创建或验证 `vector` 扩展。因此没有 pgvector 的普通 PostgreSQL 也会被报告为健康。
- 这与首页“实时探针”“不会伪装成正常”的文案（`frontend/src/app/App.tsx:83-100`、`frontend/src/app/App.tsx:239-248`）及阶段 1 的真实状态要求冲突。

命令证据：

```text
probe_model(Settings(chat_base_url='http://127.0.0.1:9/v1', chat_model='missing-model'))
=> {'state': 'healthy', ...}

probe_obsidian(Settings(vault_path='.'))
=> {'state': 'healthy', 'detail': 'Vault 可访问；监听器骨架已就绪', ...}
```

修复要求：状态键必须对应可验证能力。模型连接未探测时只能报告 `configured`/`unknown`（需要扩展状态模型），或执行有严格超时且不发送用户数据的能力探测；Obsidian 监听器在真实监听任务启动并有心跳前不能为健康；PostgreSQL 探针必须同时验证 `vector` 扩展。

#### P1-02 500 错误的请求关联链路不完整，并重复记录完整异常

- 请求中间件只在 `call_next` 正常返回后写 `X-Request-ID`（`backend/app/main.py:54-77`）。未处理异常在 `backend/app/main.py:61-68` 被重新抛出，因此全局 500 处理器返回的响应没有该响应头。
- `backend/app/core/errors.py:48-58` 会再记录一次同一异常，导致同一个 500 产生两份完整堆栈。
- 现有测试只覆盖 404（`backend/tests/test_health.py:23-29`），没有覆盖 422 和 500。

命令证据：临时向 `create_app()` 注册抛出 `ZeroDivisionError` 的路由后，`TestClient(..., raise_server_exceptions=False)` 返回：

```text
500 None {'error': {'code': 'internal_error', ..., 'request_id': '<uuid>'}}
```

其中 `None` 是响应头中的 `X-Request-ID`。这不满足统一错误格式和结构化日志的阶段 1 目标。

#### P1-03 官方运行命令会把 Artifact 相对路径解析到错误目录

- `.env.example:17` 配置 `ARTIFACT_ROOT=./data/artifacts`。
- `backend/app/core/config.py:21` 原样保留相对路径。
- 官方命令使用 `uv run --directory backend ...`，进程工作目录是 `backend/`。

命令证据：

```text
uv run --directory backend python -c "..."
cwd           => D:\Work\zhiliutai\backend
artifact_root => D:\Work\zhiliutai\backend\data\artifacts
```

实际受 Git 忽略和文档约定的目录是仓库根目录下的 `data/artifacts`。阶段 2 将开始写 Artifact，若不先确定统一的绝对解析基准，会写到未忽略、未规划的 `backend/data/artifacts`，形成数据泄漏和双目录风险。

#### P1-04 前端自动化测试不能证明阶段 1 的页面行为

- `frontend/tests/smoke.test.mjs:5-9` 只读取 `index.html` 并检查两个字符串，不渲染 React。
- 它没有验证 dashboard 请求、健康状态显示、七个导航入口、快速采集反馈、错误降级、1024×768 横向溢出或控制台错误。
- 审查前的 `docs/STATUS.md` 曾声称这些浏览器检查已通过，但仓库内没有可重复的测试或证据产物；本次已纠正该状态。

本轮 `curl` 已确认 Vite 首页返回 200，源码和 CSS 也包含所需布局；但 Browser 运行器因本机沙箱初始化错误无法连接，所以不能把先前人工声明当作本轮独立复验。进入阶段 2 前应增加最小组件/浏览器回归，并在可用浏览器环境中重新执行。

#### P1-05 阶段 0 基线会把 TypeScript 构建缓存当作普通未跟踪文件

- `npm --prefix frontend run build` 生成 `frontend/tsconfig.app.tsbuildinfo`。
- `.gitignore:20-23` 只忽略 `node_modules`、`dist` 和 `.vite`，没有忽略 `*.tsbuildinfo`。
- `git status --short --branch --untracked-files=all` 显示该文件为 `?? frontend/tsconfig.app.tsbuildinfo`；`git check-ignore` 也没有匹配它。

当前仓库尚无 commit，所有项目文件都是未跟踪状态。此决定本身符合 ADR-0003，但在建立首个用户授权的基线前必须排除生成文件，否则后续审查无法稳定区分源码与构建产物。

#### P1-06 阶段 1 的基础设施在线验收尚未完成

- 当前 `docker --version` 失败，PostgreSQL/pgvector 和 Redis 容器无法启动；`docs/ARCHITECTURE.md:25` 也明确说明只能在具备 Docker 的机器上验收。
- 实际 `/api/health` 返回 PostgreSQL、Redis 为 `degraded`。这证明降级路径工作，但没有证明 Compose、在线 Alembic、pgvector 扩展和 healthy 路径工作。
- `uv run --directory backend alembic upgrade head --sql` 只验证了离线 SQL 生成，不能替代对真实 PostgreSQL 的 `upgrade head`。

这是环境验收门禁，不要求自行安装 Docker。可在具备 Docker 的机器上复验，或连接用户明确提供的本机 PostgreSQL/pgvector 与 Redis；在此之前阶段 1 不应标记为通过。

### P2：可以以后优化

#### P2-01 文档存在状态漂移

- `README.md:7` 仍写阶段 0/1“正在实施”，与已完成实现及本次审查状态不一致。
- 审查前的 `docs/STATUS.md` 使用不存在的 `ZHILIU_VAULT_PATH`，而 `.env.example:15` 和 `backend/app/core/config.py:19` 使用 `VAULT_PATH`。本次已在 STATUS 中纠正，但 README 仍应由 Luna 在批次 0 更新。

#### P2-02 Compose 镜像标签不是不可变版本

`compose.yaml:5` 的 `pgvector/pgvector:pg16` 和 `compose.yaml:22` 的 `redis:7.4-alpine` 会随上游发布漂移。首版本地开发可以使用，但若要兑现严格的从空环境复现，应至少固定补丁标签，并在升级时显式验证；是否固定 digest 可留到发布前。

#### P2-03 前端文件已接近继续扩展的维护上限

`frontend/src/app/App.tsx` 约 400 行，`frontend/src/styles/global.css` 超过 1000 行。当前阶段仍可读，不值得单独大重构；阶段 2 添加收件箱和审核 UI 时，应只提取被新功能触及的页面、健康组件和布局样式。

#### P2-04 客户端请求 ID 和前端请求缺少边界

- `backend/app/main.py:56` 原样信任客户端 `X-Request-ID`，未限制长度或字符集。当前仅绑定本机，风险较低；增加规范化/长度上限可减少日志污染。
- `frontend/src/services/api.ts:36-43` 没有超时或取消机制。阶段 2 增加写请求前应引入统一 API 错误解析和超时策略。

## 4. 已通过的检查

以下结果在 2026-08-26 本轮实际执行：

```text
Python 3.12.10
Node v24.18.0
uv 0.12.2
npm 11.16.0

uv sync --project backend --locked
=> Resolved 58 packages; Checked 56 packages

npm --prefix frontend ci --ignore-scripts --no-audit --no-fund
=> added 69 packages

uv lock --project backend --check
=> passed

uv run --directory backend pytest -q
=> 3 passed

uv run --directory backend ruff check app tests
=> All checks passed

uv run --directory backend alembic upgrade head --sql
=> generated baseline SQL

npm --prefix frontend run typecheck
=> passed

npm --prefix frontend run test
=> 1 passed (HTML string smoke only)

npm --prefix frontend run build
=> Vite production build passed
```

API 与 Vite 均成功绑定 `127.0.0.1`。实际 HTTP 检查确认：

- `GET /api/health`：200，API healthy，PostgreSQL/Redis degraded，Obsidian/model not_configured。
- `GET /api/dashboard`：200，返回真实健康快照和零值首页数据。
- 不存在的 API：404，统一错误对象与 `X-Request-ID` 一致。
- Vite 首页：200。

`.env`、`.env.local`、`data/artifacts/*`、`data/volumes/*`、`data/vaults/*`、`node_modules` 和 `backend/.venv` 均被 Git 忽略；仓库中只发现 `.env.example`，秘密扫描只命中示例占位值和空 API Key 字段。

## 5. 验收门禁

只有同时满足以下条件，才把阶段 0/1 改为“通过”并开始阶段 2 业务实现：

1. P1-01 至 P1-05 由 Luna 修复并增加对应自动化测试。
2. 在真实 PostgreSQL/pgvector 与 Redis 上执行在线迁移，确认 `vector` 扩展、连接和健康状态。
3. 在浏览器中重新验证首页、导航、快速入口、API 降级、1024×768 无横向溢出和 console error 为 0；最好将核心行为固化为自动化测试。
4. 确认阶段 2 测试只使用临时 Vault；真实 Vault 路径由用户明确提供，不由实现者猜测。
5. 用户若希望建立 Git 基线，再单独授权 commit；Luna 和 Sol 均不得自行提交。

## 6. Sol 使用边界

阶段 2 的常规实现全部交给 Luna。只有两类检查需要 Sol：

1. Vault 路径边界、原子写入、Frontmatter 身份和冲突保护的定向安全/数据所有权审查。
2. 文件监听去抖、回环抑制、重命名/删除语义和当前版本原子切换的一致性审查。

普通 CRUD、迁移、React 页面、测试、文档和明确错误修复不需要 Sol。
