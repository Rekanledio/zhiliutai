import { FormEvent, useEffect, useState } from "react";

import {
  ApiError,
  type ItemFilters,
  type KnowledgeItem,
  type ObsidianStatus,
  deleteItem,
  getItem,
  getItems,
  getObsidianStatus,
  openObsidian,
  patchItem,
  reprocessItem,
  rescanObsidian,
} from "../../services/api";

type FilterForm = {
  status: string;
  sourceType: string;
  tag: string;
  collection: string;
  createdAfter: string;
  createdBefore: string;
};

const sourceLabels: Record<string, string> = {
  text: "文本",
  markdown: "Markdown",
  pdf: "PDF",
  docx: "DOCX",
  webpage: "静态网页",
  video: "视频",
};

const statusLabels: Record<string, string> = {
  processing: "处理中",
  pending_review: "待审核",
  reviewed: "待发布",
  published: "已发布",
  failed: "未完成",
  deleted: "已删除",
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status;
}

function sourceLabel(sourceType: string): string {
  return sourceLabels[sourceType] ?? sourceType;
}

function formatDate(value: string | undefined): string {
  if (!value) return "时间未知";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}

function safeManagedDirectory(value: string | null | undefined): string {
  if (!value || value.includes("\\") || value.startsWith("/") || /^[A-Za-z]:/.test(value)) {
    return "受管理目录已隐藏";
  }
  return value;
}

function displayMetadata(value: unknown): string {
  if (Array.isArray(value)) {
    if (value.every((item) => typeof item === "string")) {
      return value.join("、") || "无";
    }
    return `${value.length} 项`;
  }
  if (value !== null && typeof value === "object") {
    return "已保存";
  }
  return String(value);
}

function filtersFromForm(form: FilterForm): ItemFilters {
  return {
    status: form.status || undefined,
    sourceType: form.sourceType || undefined,
    tag: form.tag.trim() || undefined,
    collection: form.collection.trim() || undefined,
    createdAfter: form.createdAfter || undefined,
    createdBefore: form.createdBefore || undefined,
  };
}

function emptyFilterForm(): FilterForm {
  return {
    status: "published",
    sourceType: "",
    tag: "",
    collection: "",
    createdAfter: "",
    createdBefore: "",
  };
}

export function KnowledgePage() {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowledgeItem | null>(null);
  const [obsidian, setObsidian] = useState<ObsidianStatus | null>(null);
  const [draftFilters, setDraftFilters] = useState<FilterForm>(() => emptyFilterForm());
  const [filters, setFilters] = useState<ItemFilters>(() => filtersFromForm(emptyFilterForm()));
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editBody, setEditBody] = useState("");
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    Promise.all([getItems(filters), getObsidianStatus()])
      .then(([nextItems, nextStatus]) => {
        if (!active) return;
        setItems(nextItems);
        setObsidian(nextStatus);
        setSelectedId((current) =>
          current && nextItems.some((item) => item.id === current)
            ? current
            : nextItems[0]?.id ?? null,
        );
      })
      .catch((requestError) => {
        if (active) setError(errorMessage(requestError, "知识库加载失败"));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [filters, reloadToken]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    setError(null);
    getItem(selectedId)
      .then((nextDetail) => {
        if (!active) return;
        setDetail(nextDetail);
        setEditTitle(nextDetail.title);
        setEditBody(nextDetail.body ?? "");
      })
      .catch((requestError) => {
        if (active) setError(errorMessage(requestError, "知识详情加载失败"));
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedId]);

  const refresh = () => setReloadToken((value) => value + 1);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters(filtersFromForm(draftFilters));
    setNotice(null);
  };

  const clearFilters = () => {
    const cleared = emptyFilterForm();
    setDraftFilters(cleared);
    setFilters(filtersFromForm(cleared));
    setNotice(null);
  };

  const reloadDetail = async () => {
    if (!detail) return;
    setDetailLoading(true);
    setError(null);
    try {
      const nextDetail = await getItem(detail.id);
      setDetail(nextDetail);
      setEditTitle(nextDetail.title);
      setEditBody(nextDetail.body ?? "");
    } catch (requestError) {
      setError(errorMessage(requestError, "知识详情加载失败"));
    } finally {
      setDetailLoading(false);
    }
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!detail || busy) return;
    if (!editTitle.trim() || !editBody.trim()) {
      setError("标题和 Markdown 正文不能为空");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await patchItem(detail.id, {
        title: editTitle.trim(),
        body: editBody,
        expectedContentHash: detail.content_hash,
      });
      setDetail(updated);
      setEditTitle(updated.title);
      setEditBody(updated.body ?? "");
      setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setNotice("Markdown 已保存，并已通过内容哈希校验");
    } catch (requestError) {
      setError(
        requestError instanceof ApiError && requestError.code === "content_conflict"
          ? "保存失败：Obsidian 内容已变化，请重新载入后再编辑。"
          : errorMessage(requestError, "知识内容保存失败"),
      );
    } finally {
      setBusy(false);
    }
  };

  const reprocess = async () => {
    if (!detail || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const submission = await reprocessItem(detail.id);
      setNotice(`已提交重处理，任务 ${submission.job_id.slice(0, 8)}… 会回到审核流程。`);
      refresh();
    } catch (requestError) {
      setError(errorMessage(requestError, "重处理提交失败"));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!detail || busy) return;
    if (!window.confirm("确定软删除这条知识吗？用户的 Markdown 正文不会被删除。")) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await deleteItem(detail.id);
      setItems((current) => current.filter((item) => item.id !== detail.id));
      setSelectedId(null);
      setDetail(null);
      setNotice("知识条目已软删除，Markdown 正文仍保留在 Obsidian");
    } catch (requestError) {
      setError(errorMessage(requestError, "知识条目删除失败"));
    } finally {
      setBusy(false);
    }
  };

  const open = async () => {
    if (!detail) return;
    try {
      const { uri } = await openObsidian(detail.id);
      window.location.href = uri;
    } catch (requestError) {
      setError(errorMessage(requestError, "无法打开 Obsidian"));
    }
  };

  const rescan = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await rescanObsidian();
      setNotice(`扫描完成：更新 ${result.changed ?? 0}，冲突 ${result.conflicts ?? 0}`);
      refresh();
      if (detail) await reloadDetail();
    } catch (requestError) {
      setError(errorMessage(requestError, "扫描失败"));
    } finally {
      setBusy(false);
    }
  };

  const isEditable = Boolean(detail?.status === "published" && !detail.pending_content_version_id);
  const publicMetadata = detail?.source_metadata
    ? Object.entries(detail.source_metadata).filter(([key]) => key !== "segments")
    : [];

  return (
    <section className="stage-page knowledge-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">Obsidian Source of Truth</span>
          <h1>知识库</h1>
          <p>列表只展示数据库确认的状态；正文直接来自受管理 Markdown，索引可重建。</p>
        </div>
        <button className="ghost-button" type="button" onClick={() => void rescan()} disabled={busy}>
          重新扫描
        </button>
      </div>

      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {notice ? <div className="inline-message" role="status">{notice}</div> : null}

      <div className="obsidian-strip">
        <strong>Obsidian</strong>
        <span>
          {!obsidian?.configured
            ? "未配置"
            : obsidian.watcher_running
              ? "监听中 · " + safeManagedDirectory(obsidian.managed_directory)
              : "已配置，监听器未运行"}
        </span>
      </div>

      <form className="knowledge-filters" onSubmit={applyFilters}>
        <div className="knowledge-filter-grid">
          <label>
            状态
            <select
              aria-label="知识状态"
              value={draftFilters.status}
              onChange={(event) => setDraftFilters((current) => ({ ...current, status: event.target.value }))}
            >
              <option value="">全部状态</option>
              <option value="published">已发布</option>
              <option value="pending_review">待审核</option>
              <option value="reviewed">待发布</option>
              <option value="processing">处理中</option>
              <option value="failed">未完成</option>
            </select>
          </label>
          <label>
            来源
            <select
              aria-label="知识来源"
              value={draftFilters.sourceType}
              onChange={(event) => setDraftFilters((current) => ({ ...current, sourceType: event.target.value }))}
            >
              <option value="">全部来源</option>
              {Object.entries(sourceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
          <label>
            标签
            <input
              aria-label="筛选标签"
              value={draftFilters.tag}
              onChange={(event) => setDraftFilters((current) => ({ ...current, tag: event.target.value }))}
              placeholder="确认标签"
              maxLength={80}
            />
          </label>
          <label>
            合集
            <input
              aria-label="筛选合集"
              value={draftFilters.collection}
              onChange={(event) => setDraftFilters((current) => ({ ...current, collection: event.target.value }))}
              placeholder="人工合集"
              maxLength={200}
            />
          </label>
          <label>
            起始日期
            <input
              aria-label="起始日期"
              type="date"
              value={draftFilters.createdAfter}
              onChange={(event) => setDraftFilters((current) => ({ ...current, createdAfter: event.target.value }))}
            />
          </label>
          <label>
            结束日期
            <input
              aria-label="结束日期"
              type="date"
              value={draftFilters.createdBefore}
              onChange={(event) => setDraftFilters((current) => ({ ...current, createdBefore: event.target.value }))}
            />
          </label>
        </div>
        <div className="knowledge-filter-actions">
          <span>已排除软删除条目和失效 current 版本。</span>
          <button className="text-button" type="button" onClick={clearFilters} disabled={busy}>清除条件</button>
          <button className="primary-button" type="submit" disabled={busy}>应用筛选</button>
        </div>
      </form>

      <div className="knowledge-layout">
        <aside className="knowledge-results" aria-label="知识条目列表">
          <div className="knowledge-results-heading">
            <div>
              <span className="eyebrow">Published Knowledge</span>
              <h2>知识条目 <small>{items.length}</small></h2>
            </div>
          </div>
          {loading ? (
            <p className="quiet-note">正在加载知识库…</p>
          ) : items.length === 0 ? (
            <p className="quiet-note">没有符合条件的知识条目；审核并发布后的 Markdown 会出现在这里。</p>
          ) : (
            <div className="knowledge-result-list">
              {items.map((item) => (
                <button
                  className={"knowledge-result-item " + (item.id === selectedId ? "is-selected" : "")}
                  type="button"
                  key={item.id}
                  onClick={() => setSelectedId(item.id)}
                  disabled={busy}
                >
                  <strong>{item.title}</strong>
                  <span>{sourceLabel(item.source_type)} · {statusLabel(item.status)}</span>
                  <small>{formatDate(item.updated_at)}</small>
                  {item.confirmed_tags && item.confirmed_tags.length > 0 ? (
                    <small>标签：{item.confirmed_tags.join("、")}</small>
                  ) : null}
                </button>
              ))}
            </div>
          )}
        </aside>

        <article className="knowledge-detail" aria-label="知识详情">
          {detailLoading ? (
            <div className="knowledge-empty-state"><strong>正在打开知识详情…</strong></div>
          ) : detail ? (
            <>
              <div className="knowledge-detail-heading">
                <div>
                  <span className="eyebrow">当前知识</span>
                  <h2>{detail.title}</h2>
                  <p>{sourceLabel(detail.source_type)} · {statusLabel(detail.status)} · v{detail.version_no ?? "—"}</p>
                </div>
                <span className={"knowledge-sync-badge " + (detail.sync_state ?? "unknown")}>
                  同步：{detail.sync_state ?? "未绑定"}
                </span>
              </div>

              <div className="knowledge-meta-grid">
                <div><span>来源</span><strong>{sourceLabel(detail.source_type)}</strong></div>
                <div><span>更新时间</span><strong>{formatDate(detail.updated_at)}</strong></div>
                <div><span>内容哈希</span><strong>{detail.content_hash.slice(0, 12)}…</strong></div>
                <div><span>Markdown</span><strong>{detail.note_relative_path ? "受管理笔记" : "尚未绑定"}</strong></div>
              </div>

              {detail.pending_content_version_id ? (
                <div className="knowledge-pending-note" role="note">
                  当前条目有待审核版本；知识库暂不允许覆盖它，请先在收件箱完成审核与发布。
                </div>
              ) : null}

              <form className="knowledge-edit-form" onSubmit={(event) => void save(event)}>
                <label>
                  标题
                  <input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} maxLength={300} disabled={!isEditable || busy} />
                </label>
                <label>
                  Markdown 正文
                  <textarea aria-label="Markdown 正文" value={editBody} onChange={(event) => setEditBody(event.target.value)} rows={15} disabled={!isEditable || busy} />
                </label>
                <div className="knowledge-edit-actions">
                  <span>{isEditable ? "保存时会校验 expected_content_hash，防止覆盖 Obsidian 外部修改。" : "只有无待审版本的已发布条目可在此编辑。"}</span>
                  <button className="primary-button" type="submit" disabled={!isEditable || busy}>保存 Markdown</button>
                </div>
              </form>

              <div className="knowledge-organization">
                <div>
                  <span className="eyebrow">已确认组织信息</span>
                  <strong>标签</strong>
                  <p>{detail.confirmed_tags?.length ? detail.confirmed_tags.join("、") : "暂无已确认标签"}</p>
                </div>
                <div>
                  <strong>合集</strong>
                  <p>{detail.collections?.length ? detail.collections.join("、") : "暂无所属合集"}</p>
                </div>
              </div>

              {publicMetadata.length > 0 ? (
                <div className="knowledge-source-card">
                  <span className="eyebrow">可公开来源信息</span>
                  <dl>
                    {publicMetadata.map(([key, value]) => (
                      <div key={key}><dt>{key}</dt><dd>{displayMetadata(value)}</dd></div>
                    ))}
                  </dl>
                </div>
              ) : null}

              <div className="knowledge-detail-actions">
                <button className="ghost-button" type="button" onClick={() => void open()} disabled={busy || !detail.note_relative_path || detail.status !== "published"}>在 Obsidian 打开 ↗</button>
                <button className="ghost-button" type="button" onClick={() => void reloadDetail()} disabled={busy}>重新载入</button>
                <button className="ghost-button" type="button" onClick={() => void reprocess()} disabled={busy || detail.status === "deleted" || Boolean(detail.pending_content_version_id)}>重处理</button>
                <button className="ghost-button danger-button" type="button" onClick={() => void remove()} disabled={busy}>软删除</button>
              </div>
            </>
          ) : (
            <div className="knowledge-empty-state">
              <span className="placeholder-icon">▤</span>
              <strong>选择一条知识查看详情</strong>
              <p>详情会展示来源、正文、已确认标签/合集和安全的 Citation 来源信息。</p>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
