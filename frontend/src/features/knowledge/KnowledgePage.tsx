import { useEffect, useState } from "react";

import {
  ApiError,
  type KnowledgeItem,
  type ObsidianStatus,
  getItems,
  getObsidianStatus,
  openObsidian,
  rescanObsidian,
} from "../../services/api";

export function KnowledgePage() {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [obsidian, setObsidian] = useState<ObsidianStatus | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = async () => {
    const [nextItems, nextStatus] = await Promise.all([
      getItems(),
      getObsidianStatus(),
    ]);
    setItems(nextItems.filter((item) => item.status === "published"));
    setObsidian(nextStatus);
  };

  useEffect(() => {
    void refresh().catch((error) =>
      setMessage(error instanceof ApiError ? error.message : "知识库加载失败"),
    );
  }, []);

  const rescan = async () => {
    try {
      const result = await rescanObsidian();
      setMessage(
        "扫描完成：更新 " + (result.changed ?? 0) + "，冲突 " + (result.conflicts ?? 0),
      );
      await refresh();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : "扫描失败");
    }
  };

  const open = async (id: string) => {
    try {
      const { uri } = await openObsidian(id);
      window.location.href = uri;
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : "无法打开 Obsidian");
    }
  };

  return (
    <section className="stage-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">Obsidian Source of Truth</span>
          <h1>知识库</h1>
          <p>正文直接来自受管理 Markdown；数据库和索引可重建。</p>
        </div>
        <button className="ghost-button" type="button" onClick={() => void rescan()}>
          重新扫描
        </button>
      </div>
      <div className="obsidian-strip">
        <strong>Obsidian</strong>
        <span>
          {!obsidian?.configured
            ? "未配置"
            : obsidian.watcher_running
              ? "监听中 · " + obsidian.managed_directory
              : "已配置，监听器未运行"}
        </span>
      </div>
      {message ? <div className="inline-message" role="status">{message}</div> : null}
      <div className="knowledge-list">
        {items.length === 0 ? (
          <p className="quiet-note">审核并发布后的知识会出现在这里。</p>
        ) : (
          items.map((item) => (
            <article className="knowledge-row" key={item.id}>
              <div>
                <strong>{item.title}</strong>
                <span>{item.source_type} · {item.sync_state ?? "synced"}</span>
              </div>
              <button className="text-button" type="button" onClick={() => void open(item.id)}>
                在 Obsidian 打开 ↗
              </button>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
