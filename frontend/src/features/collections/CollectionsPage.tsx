import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  type Collection,
  type CollectionSummary,
  type KnowledgeItem,
  addCollectionItem,
  createCollection,
  deleteCollection,
  getCollection,
  getCollections,
  getItems,
  removeCollectionItem,
  updateCollection,
} from "../../services/api";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function summaryFromCollection(collection: Collection): CollectionSummary {
  return {
    id: collection.id,
    name: collection.name,
    description: collection.description,
    item_count: collection.item_count,
    moc_enabled: collection.moc_enabled,
  };
}

export function CollectionsPage() {
  const [collections, setCollections] = useState<CollectionSummary[]>([]);
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Collection | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [itemToAdd, setItemToAdd] = useState("");

  const refresh = async (preferredId?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const [nextCollections, nextItems] = await Promise.all([
        getCollections(),
        getItems("published"),
      ]);
      const nextId =
        preferredId !== undefined
          ? preferredId
          : selectedId && nextCollections.some((item) => item.id === selectedId)
            ? selectedId
            : nextCollections[0]?.id ?? null;
      setCollections(nextCollections);
      setItems(
        nextItems.filter(
          (item) => item.status === "published" && Boolean(item.current_content_version_id),
        ),
      );
      setSelectedId(nextId);
      if (nextId) {
        setDetailLoading(true);
        const nextDetail = await getCollection(nextId);
        setDetail(nextDetail);
        setEditName(nextDetail.name);
        setEditDescription(nextDetail.description ?? "");
      } else {
        setDetail(null);
      }
    } catch (requestError) {
      setError(errorMessage(requestError, "合集加载失败"));
    } finally {
      setDetailLoading(false);
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const availableItems = useMemo(() => {
    const memberIds = new Set(detail?.items.map((item) => item.id) ?? []);
    return items.filter((item) => !memberIds.has(item.id));
  }, [detail, items]);

  const selectCollection = async (id: string) => {
    setSelectedId(id);
    setDetailLoading(true);
    setError(null);
    try {
      const nextDetail = await getCollection(id);
      setDetail(nextDetail);
      setEditName(nextDetail.name);
      setEditDescription(nextDetail.description ?? "");
      setItemToAdd("");
    } catch (requestError) {
      setError(errorMessage(requestError, "合集详情加载失败"));
    } finally {
      setDetailLoading(false);
    }
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!newName.trim() || busy) {
      setError("请填写合集名称");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await createCollection(newName.trim(), newDescription.trim() || undefined);
      setCollections((current) => [...current, summaryFromCollection(created)]);
      setSelectedId(created.id);
      setDetail(created);
      setEditName(created.name);
      setEditDescription(created.description ?? "");
      setNewName("");
      setNewDescription("");
      setNotice("合集已创建");
    } catch (requestError) {
      setError(errorMessage(requestError, "合集创建失败"));
    } finally {
      setBusy(false);
    }
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!detail || !editName.trim() || busy) {
      setError("请填写合集名称");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await updateCollection(detail.id, {
        name: editName.trim(),
        description: editDescription.trim() || null,
      });
      setDetail(updated);
      setCollections((current) =>
        current.map((item) => (item.id === updated.id ? summaryFromCollection(updated) : item)),
      );
      setEditName(updated.name);
      setEditDescription(updated.description ?? "");
      setNotice("合集信息已保存");
    } catch (requestError) {
      setError(errorMessage(requestError, "合集更新失败"));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!detail || busy) {
      return;
    }
    if (!window.confirm("确定删除这个合集吗？知识条目和 Markdown 不会被删除。")) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await deleteCollection(detail.id);
      const remaining = collections.filter((item) => item.id !== detail.id);
      setCollections(remaining);
      const nextId = remaining[0]?.id ?? null;
      setSelectedId(nextId);
      if (nextId) {
        const nextDetail = await getCollection(nextId);
        setDetail(nextDetail);
        setEditName(nextDetail.name);
        setEditDescription(nextDetail.description ?? "");
      } else {
        setDetail(null);
      }
      setNotice("合集已删除");
    } catch (requestError) {
      setError(errorMessage(requestError, "合集删除失败"));
    } finally {
      setBusy(false);
    }
  };

  const handleAdd = async () => {
    if (!detail || !itemToAdd || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await addCollectionItem(detail.id, itemToAdd);
      setDetail(updated);
      setCollections((current) =>
        current.map((item) => (item.id === updated.id ? summaryFromCollection(updated) : item)),
      );
      setItemToAdd("");
      setNotice("知识条目已加入合集");
    } catch (requestError) {
      setError(errorMessage(requestError, "加入合集失败"));
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (itemId: string) => {
    if (!detail || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await removeCollectionItem(detail.id, itemId);
      setDetail(updated);
      setCollections((current) =>
        current.map((item) => (item.id === updated.id ? summaryFromCollection(updated) : item)),
      );
      setNotice("知识条目已移出合集");
    } catch (requestError) {
      setError(errorMessage(requestError, "移出合集失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="stage-page collections-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">人工合集</span>
          <h1>合集</h1>
          <p>把已发布知识组织成可维护的主题集合，正文仍以 Obsidian Markdown 为准。</p>
        </div>
        <div className="collection-boundary-note" role="note">
          <strong>Frontmatter 同步</strong>
          <span>成员变更会同步到受管理 Markdown 的 collections</span>
        </div>
      </div>

      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {notice ? <div className="inline-message" role="status">{notice}</div> : null}

      <div className="collections-layout">
        <aside className="collection-panel" aria-label="合集列表">
          <div className="collection-panel-heading">
            <div>
              <span className="eyebrow">Collections</span>
              <h2>我的合集</h2>
            </div>
            <span className="collection-count">{collections.length}</span>
          </div>
          <form className="collection-create-form" onSubmit={(event) => void handleCreate(event)}>
            <label htmlFor="new-collection-name">新合集名称</label>
            <input
              id="new-collection-name"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              maxLength={200}
              placeholder="例如：读书笔记"
              disabled={busy}
            />
            <label htmlFor="new-collection-description">说明（可选）</label>
            <textarea
              id="new-collection-description"
              value={newDescription}
              onChange={(event) => setNewDescription(event.target.value)}
              maxLength={2000}
              rows={2}
              placeholder="这个合集想整理什么？"
              disabled={busy}
            />
            <button className="primary-button" type="submit" disabled={busy || !newName.trim()}>
              创建合集
            </button>
          </form>
          <div className="collection-list" aria-live="polite">
            {loading ? (
              <p className="quiet-note">正在加载合集…</p>
            ) : collections.length === 0 ? (
              <p className="quiet-note">还没有合集，先创建一个主题集合。</p>
            ) : (
              collections.map((collection) => (
                <button
                  className={"collection-list-item " + (collection.id === selectedId ? "is-selected" : "")}
                  key={collection.id}
                  type="button"
                  onClick={() => void selectCollection(collection.id)}
                  disabled={busy}
                >
                  <span>
                    <strong>{collection.name}</strong>
                    <small>{collection.item_count} 个已发布条目</small>
                  </span>
                  <span aria-hidden="true">›</span>
                </button>
              ))
            )}
          </div>
        </aside>

        <div className="collection-detail-panel">
          {detailLoading ? (
            <div className="collection-empty-state"><strong>正在打开合集…</strong></div>
          ) : detail ? (
            <>
              <div className="collection-detail-heading">
                <div>
                  <span className="eyebrow">人工维护</span>
                  <h2>{detail.name}</h2>
                  <p>只展示已发布且当前版本有效的知识条目。</p>
                </div>
                <span className="collection-moc-status">MOC：未启用</span>
              </div>

              <form className="collection-edit-form" onSubmit={(event) => void handleUpdate(event)}>
                <div className="collection-form-grid">
                  <label>
                    合集名称
                    <input
                      value={editName}
                      onChange={(event) => setEditName(event.target.value)}
                      maxLength={200}
                      disabled={busy}
                    />
                  </label>
                  <label>
                    合集说明
                    <textarea
                      value={editDescription}
                      onChange={(event) => setEditDescription(event.target.value)}
                      maxLength={2000}
                      rows={2}
                      disabled={busy}
                    />
                  </label>
                </div>
                <div className="form-actions">
                  <span>成员关系会同步回 Markdown Frontmatter。</span>
                  <button className="ghost-button danger-button" type="button" onClick={() => void handleDelete()} disabled={busy}>
                    删除合集
                  </button>
                  <button className="primary-button" type="submit" disabled={busy || !editName.trim()}>
                    保存修改
                  </button>
                </div>
              </form>

              <div className="collection-members-card">
                <div className="collection-section-heading">
                  <div>
                    <span className="eyebrow">已确认内容</span>
                    <h3>知识条目 <small>{detail.items.length}</small></h3>
                  </div>
                  <div className="collection-add-row">
                    <label className="sr-only" htmlFor="collection-item-select">选择已发布知识条目</label>
                    <select
                      id="collection-item-select"
                      value={itemToAdd}
                      onChange={(event) => setItemToAdd(event.target.value)}
                      disabled={busy || availableItems.length === 0}
                    >
                      <option value="">加入已发布条目</option>
                      {availableItems.map((item) => (
                        <option value={item.id} key={item.id}>{item.title}</option>
                      ))}
                    </select>
                    <button className="primary-button" type="button" onClick={() => void handleAdd()} disabled={busy || !itemToAdd}>
                      加入
                    </button>
                  </div>
                </div>
                {detail.items.length === 0 ? (
                  <p className="quiet-note">还没有成员；只能加入已发布的知识条目。</p>
                ) : (
                  <div className="collection-member-list">
                    {detail.items.map((item) => (
                      <article className="collection-member-row" key={item.id}>
                        <div>
                          <strong>{item.title}</strong>
                          <span>{item.source_type} · v{item.version_no}</span>
                          {item.suggested_tags.length > 0 ? (
                            <small>标签：{item.suggested_tags.join("、")}</small>
                          ) : null}
                        </div>
                        <button className="text-button" type="button" onClick={() => void handleRemove(item.id)} disabled={busy}>
                          移除
                        </button>
                      </article>
                    ))}
                  </div>
                )}
              </div>

              <div className="collection-tags-card">
                <span className="eyebrow">相关标签</span>
                {detail.related_tags.length === 0 ? (
                  <p>当前成员还没有已确认标签。</p>
                ) : (
                  <div className="collection-tag-list">
                    {detail.related_tags.map((tag) => <span key={tag}>{tag}</span>)}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="collection-empty-state">
              <span className="placeholder-icon">◇</span>
              <strong>从一个人工合集开始</strong>
              <p>创建合集后，可以从已发布知识中手动选择成员。</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
