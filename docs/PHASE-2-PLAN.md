# 阶段 2 实施与验收记录：Text / Markdown + Obsidian

更新时间：2026-08-26
需求基线：`docs/PROJECT.md`

## 1. 目标

~~~text
提交 Text / Markdown
→ 持久化任务
→ 草稿与人工审核
→ 原子写入 Obsidian
→ Chunk + FTS5 + Qdrant
→ Obsidian 外部修改
→ watcher/rescan
→ 新 ContentVersion
→ 可靠切换最新索引
~~~

阶段 2 不实现 PDF、DOCX、网页、RAG Chat 或视频。

## 2. 已完成批次

1. 架构收口：SQLite/FTS5、Qdrant Local、JobRunner；阶段 1 的 Docker/PostgreSQL/Redis blocker 被正式移除。
2. 数据模型与 migration：阶段 2 实际使用的实体和 FTS5 baseline，临时数据库 upgrade/down/up 通过。
3. 文件安全基础：内容哈希、内容寻址 Artifact、稳定 Frontmatter、受管理路径和原子写。
4. Inbox 与审核 API：Text/Markdown、去重、幂等键、草稿编辑、审核、Dashboard 真实统计。
5. 发布与索引：Obsidian Markdown、NoteBinding、Chunk、FTS5、Qdrant payload、深链。
6. Watcher 与 rescan：外部修改、重命名、缺失、重复 ID、增量版本与重索引。
7. 前端：Inbox、审核发布、Knowledge/Obsidian 状态、Jobs、统一 API 错误、超时与取消。
8. 验收：临时 Vault 端到端自动化、组件测试、构建与文档收口。

## 3. 一致性规则

- 确认后的正文只以 Obsidian Markdown 为主来源。
- Artifact 不可变且内容寻址；数据库中的正文是草稿或可重建投影。
- Vault 写入采用同目录临时文件 + flush/fsync + replace。
- 发布/同步只有在 Qdrant 与 SQLite Chunk/FTS 写入成功后才切换 current version。
- 外部修改时旧哈希 PATCH 返回 409，不自动覆盖。
- rescan 可补偿 watcher 漏事件与“文件成功、数据库失败”的中间态。
- 删除数据库条目不删除 Vault 文件。
- 单 API 进程拥有 watcher；当前不声称多进程 watcher 安全。

## 4. 验收证据

重复命令见 `docs/TESTING.md`，实际结果见 `docs/STATUS.md`。当前组件自动化通过；真实浏览器 E2E 仍是 Playwright CI/浏览器能力验证项，未执行即不标记 passed。

## 5. 后续阶段边界

阶段 2 验收完成后，下一阶段只进入 `docs/PROJECT.md` 已定义的 PDF、DOCX 与静态网页采集。Hybrid RAG、问答、视频、Agent、MCP Server/Client 仍按后续阶段顺序实施，不在阶段 2 提前扩张。
