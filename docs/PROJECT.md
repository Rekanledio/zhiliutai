# “知流台”个人知识库：最终设计与实施规划

> 状态：项目启动基线
> 用途：复制到新项目目录，作为后续 Codex 对话和开发的单一规划依据
> 参考界面：WorkBuddy「日常集」与「个人办公工作台」页面，仅借鉴布局、信息层级与交互节奏，不复制品牌、文案和素材

## 1. 最终结论

“知流台”是一个仅供个人使用、运行在本地电脑上的知识采集、整理、检索和问答系统。

- 主要入口：本地网页工作台。
- 知识编辑层：Obsidian Vault 中的 Markdown 文件。
- 检索层：自建 RAG，不依赖第三方 RAG 平台。
- 运行方式：单用户、本机服务，只监听 `127.0.0.1`。
- 模型方式：支持 OpenAI-compatible 的聊天、Embedding、转录、视觉和重排服务分别配置；也允许以后换成本地模型。因此“本地运行”不等于“强制完全离线”。
- 项目目标：既可真实长期使用，也完整覆盖 Python、LLM API、LangChain、RAG、LangGraph、Agent、MCP、FastAPI、React、SQLite、Qdrant Local、Docker 交付和测试。

项目首版不加入登录、云端业务数据库、多人协作、浏览器扩展、Kubernetes、LoRA、vLLM 和多 Agent 产品功能。

## 2. 产品原则

1. **本机优先**：原始文件、笔记、任务数据和检索索引默认都在本机。
2. **Obsidian 可脱离系统使用**：即使知流台停止运行，确认后的 Markdown 笔记仍然完整、可读、可迁移。
3. **来源与 AI 结果分离**：原件、抽取文本、AI 摘要、标签和最终笔记分别保存，模型或 Prompt 更新后可以重新处理。
4. **回答必须可验证**：引用至少包含笔记、页码、网页 URL、原文片段、视频时间戳或关键帧定位之一；证据不足时拒答。
5. **AI 不直接覆盖用户内容**：摘要、标签、合集和正文改写先进入待确认状态。
6. **先完成闭环，再增加高级能力**：每个阶段必须可以运行和验收，不提前堆叠多模态、多 Agent 等功能。

## 3. 数据所有权与 Obsidian 边界

不同数据分别指定唯一主来源，避免数据库和 Markdown 成为互相覆盖的“双主库”。

| 数据 | 主来源 | 说明 |
|---|---|---|
| 用户确认后的知识正文 | Obsidian Markdown | 在网页或 Obsidian 中编辑，最终都写入同一 Markdown 文件 |
| 原始文件、网页快照、转录、关键帧 | 本地 Artifact 目录 | 内容寻址保存，可按保留策略清理媒体 |
| 来源、处理状态、任务、引用、模型运行记录 | SQLite | 业务、元数据与可观测性数据 |
| Chunk 元数据与全文索引 | SQLite + FTS5 | 可从 Markdown 重建的派生数据 |
| Embedding 向量索引 | Qdrant Local | 只保存向量及必要关联 payload，不作为业务数据库 |
| 标签和合集 | SQLite；发布时映射到 Frontmatter | 数据库负责关系，Markdown 保留可迁移信息 |

建议的 Vault 目录由配置指定，不硬编码用户实际路径：

```text
<vault>/知流台/
├─ Inbox/          # 待人工整理的笔记
├─ Notes/          # 已确认的通用笔记
├─ Videos/         # 视频知识笔记
├─ Articles/       # 网页文章笔记
├─ Collections/    # 合集入口或 MOC 笔记
└─ Assets/         # 需要长期保留的图片和附件
```

每篇受管理笔记使用稳定 Frontmatter：

```yaml
---
zhiliu_id: "UUID"
source_type: video
source_url: "https://example.com/video"
status: reviewed
created_at: "ISO-8601"
updated_at: "ISO-8601"
tags:
  - AI
  - RAG
---
```

同步规则：

1. 知流台生成草稿，用户确认后写入 Vault。
2. 文件监听器监控受管理目录；检测到新增、修改、重命名或删除后，进入防抖队列。
3. 使用 `zhiliu_id + 内容哈希` 判断变化，重新切分并建立新索引版本。
4. 新索引全部成功后再原子切换当前版本，失败时保留旧索引。
5. 网页工作台中的“编辑笔记”直接读写对应 Markdown，不维护另一份可编辑正文。
6. 工作台提供 `obsidian://open` 深链，一键打开对应笔记。
7. Obsidian Sync 可以独立负责 Markdown 在设备间同步，但不负责同步 SQLite、Qdrant 或运行中的后端。

首版只支持一个 Vault 和一个受管理目录。自动冲突合并、多 Vault、移动端访问知流台网页均不在首版范围。

## 4. 网页工作台设计

### 4.1 视觉方向

融合两个参考页面的优点：

- 借鉴「日常集」：温和的米白背景、低饱和色块、大留白、圆角卡片、快速入口和清晰的首页节奏。
- 借鉴「个人办公工作台」：固定侧栏、状态数字、紧急信息区、高密度列表以及明显的操作反馈。
- 形成知流台自己的视觉识别：暖灰/象牙白为底色，深青色为主色，梅紫仅作 AI 或重点状态强调；不使用参考页面的 Logo、插图、原始文案和完全相同的布局比例。
- 桌面端优先，最低适配到窄屏笔记本；移动端只保证基本响应式，不作为首版验收目标。

### 4.2 导航

```text
今日总览
收件箱
知识库
合集
搜索与问答
处理任务
设置
```

侧栏底部显示：FastAPI、SQLite、Qdrant Local、Artifact Storage、Obsidian、Watcher、模型能力和 FFmpeg 状态。

### 4.3 今日总览

首页用于回答“今天有哪些资料进入了系统、哪些需要我处理、知识库是否健康”。

- 顶部问候和日期。
- 快速采集：粘贴文本、上传文件、添加网页、添加视频。
- 状态概览：知识条目数、今日新增、待确认、处理中/失败数。
- 待确认区域：摘要、标签、合集和笔记草稿的审核卡片。
- 最近知识：最近确认或编辑的条目。
- 处理中：正在转录、解析或建立索引的任务进度。
- 今日回顾：可选展示最近新增主题，不在首版做复杂统计。

### 4.4 核心页面

**收件箱**

- 统一输入文本、文件、URL 和视频 URL。
- 支持拖放、批量文件上传、即时校验、重复提示。
- 每条资料显示采集、解析、AI 处理和审核状态。

**知识库**

- 列表/卡片视图、标签、合集、来源类型、日期和处理状态筛选。
- 详情页三栏：来源与元数据、Markdown 正文、引用/关联资料。
- 支持在网页编辑 Markdown、在 Obsidian 打开、重新处理和软删除。

**合集**

- 人工合集为主，AI 只做推荐。
- 显示合集说明、知识条目、相关标签和可选 MOC 笔记。

**搜索与问答**

- 同一页面提供关键词搜索和 RAG 对话。
- 回答流式显示；引用以可展开卡片呈现。
- 点击引用可以打开对应 Markdown、PDF 页、网页来源或视频时间戳。
- 展示“为什么找到这些资料”的简要检索信息，便于学习和调试。

**处理任务**

- 显示阶段、耗时、重试次数、错误摘要和最后心跳。
- 支持安全重试、取消尚未执行的任务和查看结构化日志。

**设置**

- Vault 路径与同步状态。
- Chat、Embedding、ASR、Vision、Reranker 分别配置。
- 切分和检索参数。
- 原始媒体保留策略。
- 数据备份、重新扫描和重建索引。
- API Key 只写入后端环境变量或本机秘密配置，不返回前端。

## 5. 采集和视频处理

支持范围：纯文本、Markdown、TXT、PDF、DOCX、无需登录的静态网页，以及用户有权保存和处理的视频 URL。

通用采集流使用 LangGraph 表达：

```text
输入校验
→ 获取来源与元数据
→ 类型路由
→ 解析/转录
→ 内容哈希与去重
→ 生成草稿及标签建议
→ 人工确认
→ 写入 Obsidian
→ 切分与索引
→ 发布可检索版本
```

视频处理采用条件多模态流程：

```text
URL 安全校验与元数据
→ 优先获取已有字幕
→ 无字幕时提取音轨并转录
→ 判断视频类型
   ├─ 访谈/播客：跳过视觉处理
   └─ 幻灯片/教程：场景检测 → 关键帧去重
                    → 视觉理解 + 选择性 OCR
→ 按时间戳对齐转录和视觉事件
→ 章节划分和分层笔记
→ 审核、发布、索引
→ 按策略清理临时媒体
```

技术选择：`yt-dlp + FFmpeg`；转录服务提供统一适配器，本地 `whisper.cpp` 是可选实现而不是强制默认。视觉模型只处理筛选后的关键帧，OCR 只用于幻灯片、代码和 UI 等需要精确文字的画面。

视频派生产物包括：

```text
source.json
transcript.json / transcript.vtt
chapters.json
keyframes/*.webp
visual_events.json
note.md
processing_manifest.json
```

网络视频默认只临时保存原媒体：处理成功后按配置立即删除或保留若干天；用户主动上传的原件默认保留。始终保留来源 URL、哈希、转录、必要关键帧和处理清单。

### 5.1 LangChain 与 LangGraph 边界

LangChain 用于 Document、Loader、Text Splitter、Embedding、Vector Store/ Retriever、Prompt/Message、LLM Adapter 和合适的 Tool 抽象；普通 Python 能清晰表达的领域逻辑不额外包裹。

系统最终保留两个主要 Graph：`IngestionGraph` 与 `QuestionAnswerGraph`。Graph 负责 state、routing、conditional branch、Human-in-the-loop、必要的 checkpoint/resume 和 agentic decision；解析、知识、笔记、Embedding、检索、Citation、索引和模型调用仍由普通 service 完成，Graph Node 只编排这些 service。

## 6. 自建 RAG 设计

### 6.1 索引

- SQLite 保存业务数据、Chunk 元数据和 FTS5 全文索引；Qdrant Local 保存向量及关联 payload。
- Markdown 按标题结构切分；PDF 保留页码；网页保留 URL/标题层级；视频保留开始和结束时间戳、章节及关键帧 ID。
- 首版使用结构感知的递归切分；父子 Chunk 和语义切分作为后续实验。
- Chunk 保存 `item_id`、`content_version_id`、来源定位、文本哈希、Embedding 模型和切分策略版本。
- Qdrant payload 至少保存 `chunk_id`、`knowledge_item_id`、`content_version_id`、`source_type`、`source_locator`、`embedding_model` 和 `embedding_version`。

### 6.2 检索与回答

```text
问题分类
→ 必要时 Query Rewrite
→ 元数据过滤
→ SQLite FTS5 全文召回 + Qdrant Local 向量召回
→ RRF 融合
→ 可选 Reranker
→ 证据阈值检查
→ 带引用回答或明确拒答
```

- 默认不让 Agent 任意扩大检索范围。
- 检索结果和引用先形成结构化对象，再交给生成模型。
- 每条回答保存使用的 Chunk、模型、Prompt 版本、延迟和估算 Token。
- 建立固定中文评测集，记录 Recall@K、MRR、引用正确率、Faithfulness、延迟与成本。

## 7. 系统架构

```text
React + TypeScript + Vite
        │ REST + SSE
        ▼
FastAPI API
 ├─ Sources / Items / Review / Search / Chat / Sync
 ├─ LangGraph ingestion workflow
 ├─ LangGraph question-answer workflow
 ├─ Obsidian file adapter and watcher
 ├─ Model provider adapters
 ├─ MCP Server（Provider）
 └─ MCP Client（Consumer）
        │
        ├──────── SQLite + FTS5（业务、任务、Chunk 元数据）
        ├──────── Qdrant Local（向量索引）
        └──────── Local artifact storage + Obsidian Vault
```

建议的开发运行方式：

- 日常开发：FastAPI、Python JobRunner、Qdrant Local、SQLite 和 Frontend 全部在本机运行，不要求 Docker Desktop 或独立基础设施服务。
- 交付与 CI：提供单应用 Dockerfile；Docker build 在 GitHub Actions 或具备 Docker 的环境验收，不作为本地阶段门禁。
- 所有服务默认绑定 `127.0.0.1`；不自动开放局域网访问。

## 8. 后端模块和仓库结构

```text
zhiliutai/
├─ AGENTS.md
├─ README.md
├─ .env.example
├─ Dockerfile
├─ .dockerignore
├─ .github/workflows/
├─ backend/
│  ├─ pyproject.toml
│  ├─ app/
│  │  ├─ api/
│  │  ├─ core/
│  │  ├─ db/
│  │  ├─ models/
│  │  ├─ schemas/
│  │  ├─ services/
│  │  ├─ ingestion/
│  │  ├─ rag/
│  │  ├─ workflows/
│  │  ├─ obsidian/
│  │  ├─ providers/
│  │  ├─ jobs/
│  │  ├─ mcp/
│  │  └─ main.py
│  └─ tests/
├─ frontend/
│  ├─ src/
│  │  ├─ app/
│  │  ├─ components/
│  │  ├─ features/
│  │  ├─ pages/
│  │  ├─ services/
│  │  └─ styles/
│  └─ tests/
├─ data/
│  ├─ artifacts/
│  └─ fixtures/
├─ docs/
│  ├─ PROJECT.md
│  ├─ ARCHITECTURE.md
│  ├─ DECISIONS.md
│  ├─ STATUS.md
│  └─ TESTING.md
└─ scripts/
```

`data/` 的真实用户内容、SQLite 数据库、Qdrant 目录、`.env` 和 Vault 路径不得提交 Git。

## 9. 核心数据实体

- `KnowledgeItem`：知识条目及当前发布版本。
- `SourceArtifact`：原文件、网页快照、字幕、转录、关键帧等不可变产物。
- `ContentVersion`：可重新处理和回滚的内容版本。
- `NoteBinding`：`zhiliu_id`、Vault 相对路径、内容哈希和最后同步状态。
- `Chunk`：可检索分块、向量和引用定位。
- `ProcessingJob` / `JobAttempt`：后台任务、阶段、重试和错误。
- `Tag` / `Collection`：人工确认的组织关系。
- `Citation`：回答与证据之间的绑定。
- `ModelRun`：模型、Prompt、参数、Token、耗时和错误。

删除知识条目采用软删除；删除或重建派生索引不直接删除 Obsidian 文件，必须由用户在界面明确确认。

## 10. API 基线

```text
GET    /api/health
GET    /api/dashboard

POST   /api/sources/text
POST   /api/sources/files
POST   /api/sources/url
POST   /api/sources/video

GET    /api/jobs
GET    /api/jobs/{id}
POST   /api/jobs/{id}/retry

GET    /api/items
GET    /api/items/{id}
PATCH  /api/items/{id}
POST   /api/items/{id}/review
POST   /api/items/{id}/publish
POST   /api/items/{id}/reprocess
DELETE /api/items/{id}

POST   /api/search
POST   /api/chat/stream

GET    /api/obsidian/status
POST   /api/obsidian/rescan
POST   /api/obsidian/open/{item_id}
```

MCP Server 首版工具：`add_text`、`add_url`、`search_knowledge`、`get_item`、`list_collections`。MCP 调用复用同一服务层，不另写一套业务逻辑。项目同时实现 MCP Client，使主 Agent 可以连接用户明确配置的外部 MCP Server；MCP Provider 与 Consumer 都通过清晰 Tool 边界工作。

## 11. 安全与可靠性

- URL 只允许 `http/https`，解析 DNS 后阻止环回、链路本地、私网和云元数据地址，防止 SSRF。
- 重定向每一跳重新校验；设置连接/读取超时、内容类型、下载大小、视频时长和并发上限。
- 不读取任意本地路径；上传文件进入受控目录并使用生成的内部文件名。
- 不默认传递浏览器 Cookie，不绕过 DRM、登录、付费墙或平台权限。
- API Key 不进入日志、前端、本地 Markdown 或 Git。
- SQLite Job Table + Python JobRunner 任务幂等；关键阶段有心跳、重试、失败尝试历史和结构化错误；进程重启后可以恢复。
- 使用内容哈希去重，规范化 URL 只作为辅助；同一来源允许形成新版本。
- 提供数据库和 Artifact 备份脚本，并记录恢复演练步骤。

## 12. 分阶段实施与验收

### 阶段 0：项目基线

- 初始化 Git、目录、AGENTS.md、设计文档、环境示例和决策日志。
- 检查 Python、Node、Docker、FFmpeg 和目标 Vault 的可用性，不擅自安装或修改系统环境。
- 确定依赖版本并生成锁文件。

验收：仓库结构明确，文档与实际目录一致，秘密和用户数据不会提交。

### 阶段 1：可运行骨架和首页

- FastAPI、React、SQLite/FTS5、Qdrant Local、Artifact Storage 和 Python JobRunner 基线。
- 健康检查、数据库迁移、统一错误格式和结构化日志。
- 实现参考页面风格的静态首页与导航，再接入真实健康状态。

验收：本机不依赖 Docker 即可启动 API 与前端；SQLite、Qdrant、Artifact、Health、错误链路和组件测试通过。真实浏览器 E2E 作为 Playwright CI/浏览器能力门禁，不阻塞本地阶段推进。

### 阶段 2：文本/Markdown + Obsidian 闭环

- 文本和 Markdown 收件箱。
- 草稿审核、发布到 Vault、文件监听、增量重新索引和 Obsidian 深链。
- 内容哈希、版本和冲突保护。

验收：提交文本 → 审核 → 生成 Markdown → 在 Obsidian 修改 → 工作台发现变化并更新索引。

### 阶段 3：PDF、DOCX 和网页采集

- 解析、元数据、页码/标题引用、URL 安全和任务重试。
- 去重、版本切换、摘要、标签和合集建议。

验收：至少一份中文 PDF、一份 DOCX 和一个静态网页完成端到端处理，引用能回到原位置。

### 阶段 4：自建 RAG

- 全文 + 向量混合召回、RRF、可选重排、Query Rewrite 和证据阈值。
- SSE 流式回答、引用卡片和固定评测集。

验收：完成搜索、带引用回答、无证据拒答；评测结果可重复记录。

### 阶段 5：视频知识化

- 字幕优先、ASR 兜底、时间戳引用、章节和分层笔记。
- 第二步再加入条件关键帧、视觉理解和选择性 OCR。
- 临时媒体清理和失败恢复。

验收：字幕视频和无字幕视频各一个；回答可定位到时间戳，视觉信息测试样本可定位到关键帧。

### 阶段 6：Agent、MCP 和作品集完善

- LangGraph 问答路由、Human-in-the-loop、检查点。
- MCP Server + MCP Client、端到端测试、Playwright 浏览器测试、架构图、README 和 CI。
- 完成备份恢复、从空环境启动和安全检查。

验收：采集 → 处理 → 审核 → Obsidian → 检索 → 带引用回答 → MCP 查询完整闭环通过。

每阶段只在当前阶段验收通过后进入下一阶段，不同时启动所有模块。

## 13. 测试策略

- 单元测试：URL 安全、哈希、Frontmatter、路径边界、解析器、切分、引用定位、RRF、状态转换。
- Mock 模型测试：结构化输出异常、限流、超时、重试、幂等、证据不足。
- 集成测试：SQLite migration/FTS5、Qdrant Local、JobRunner、Vault 临时目录和索引版本切换。
- 端到端测试：文本、PDF、网页和视频的完整闭环。
- 浏览器测试：首页、上传、任务进度、审核、搜索、流式问答和错误恢复。
- 固定回归集：中文短文、Markdown、PDF、网页、字幕视频和无字幕视频。

真实 Obsidian Vault 不直接用于自动化测试；测试必须使用临时 Vault。

## 14. 5.6 Luna 与 5.6 Sol 协作规则

为了减少 Token，**新项目对话默认选择 `gpt-5.6-luna` 作为主模型**。

Luna 负责：

- 仓库检查、目录初始化和依赖配置。
- 常规 API、数据库 CRUD、Pydantic Schema 和迁移。
- React 页面、组件、样式和常规交互。
- 解析器、适配器、测试、文档和机械重构。
- 运行测试、定位明确错误和小范围修复。
- 更新 `docs/STATUS.md` 与下一步任务。

只有以下情况使用 `gpt-5.6-sol`：

- 需要修改跨越三个以上核心模块的架构或数据所有权边界。
- Obsidian 文件同步冲突、索引原子切换、JobRunner 恢复等一致性问题。
- RAG 召回质量设计、复杂 LangGraph 状态机或难以复现的并发问题。
- SSRF、路径穿越、秘密泄漏等安全审查。
- Luna 已基于证据尝试两次仍无法解决的复杂故障。
- 阶段 4、阶段 5 和最终交付的定向架构/质量审查。

协作约束：

1. Luna 先收集最小必要上下文、失败命令和相关文件，再把一个边界清楚的问题交给 Sol。
2. Sol 只审查或解决指定难点，不从头重做整个阶段。
3. 两个模型不同时修改同一文件；主模型合并结果并负责最终测试。
4. 常规代码不要为了“保险”交给 Sol；阶段审查也只提供变更摘要、关键文件和测试结果。
5. 每完成一个阶段，压缩 `docs/STATUS.md`，避免新对话反复读取全部历史。

## 15. 上下文与 Token 管理

- `docs/PROJECT.md`：稳定需求和边界，只在需求变化时修改。
- `docs/ARCHITECTURE.md`：当前真实架构，不记录过期设想。
- `docs/DECISIONS.md`：简短 ADR，记录重要取舍与理由。
- `docs/STATUS.md`：不超过约 150 行，只记录已完成、当前问题、验证命令和下一步。
- 每次只规划一个阶段；阶段内任务控制在可独立验证的小批次。
- 先使用 `rg` 定位相关文件，不默认读取整个仓库。
- 优先运行目标测试，再运行全量测试。
- 不在对话里重复粘贴大文件；用绝对路径和行号指向证据。

## 16. 暂不解决的问题

- 手机直接访问知流台网页。
- 多电脑共享同一业务数据库或向量索引。
- Obsidian Sync 冲突的自动语义合并。
- 需要登录、Cookie、付费或 DRM 的内容采集。
- 任意动态网页和微信视频号的稳定抓取。
- 所有视频默认进行视觉分析。
- 多用户、权限、在线部署和协作。

这些边界不妨碍首版形成完整、可展示的 AI 应用闭环。
