import { type FormEvent, useRef, useState } from "react";

import {
  ApiError,
  type ChatClaim,
  type Citation,
  type EvidenceAssessment,
  type SearchResponse,
  openObsidian,
  searchKnowledge,
  streamChat,
} from "../../services/api";

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.message : "检索失败，请稍后重试";
}

function locatorLabel(citation: Citation): string {
  const locator = citation.locator;
  if (locator.kind === "pdf") {
    return locator.page ? "PDF 第 " + locator.page + " 页" : "PDF 页码未记录";
  }
  if (locator.kind === "docx") {
    return locator.element === "table_row"
      ? "DOCX 表格"
      : locator.paragraph
        ? "DOCX 第 " + locator.paragraph + " 段"
        : "DOCX 定位";
  }
  if (locator.kind === "webpage") {
    return locator.url ? "网页快照" : "网页定位未记录";
  }
  if (locator.kind === "obsidian") {
    return locator.path ? "Obsidian · " + locator.path : "Obsidian 定位";
  }
  return "来源定位不可用";
}

function matchedByLabel(citation: Citation): string {
  const labels = citation.retrieval.matched_by.map((channel) =>
    channel === "fts" ? "全文" : channel === "vector" ? "向量" : channel,
  );
  return labels.length > 0 ? "命中：" + labels.join(" + ") : "命中信息未记录";
}

function CitationCard({
  citation,
  onOpenObsidian,
}: {
  citation: Citation;
  onOpenObsidian: (itemId: string) => void;
}) {
  const target = citation.target;
  return (
    <article className="citation-card">
      <div className="citation-card-heading">
        <span className="citation-label">{citation.citation_id}</span>
        <div>
          <strong>{citation.item_title}</strong>
          <span>{citation.source_type} · v{citation.version_no} · {locatorLabel(citation)}</span>
          <small>{matchedByLabel(citation)}</small>
        </div>
      </div>
      <p>{citation.excerpt}</p>
      <div className="citation-card-footer">
        <span className={"locator-status locator-" + citation.locator_status}>
          {citation.locator_status === "exact" ? "精确定位" : citation.locator_status === "fallback" ? "回退定位" : "定位不可用"}
        </span>
        {target.kind === "artifact" && target.artifact_id ? (
          <a
            href={"/api/artifacts/" + encodeURIComponent(target.artifact_id)}
            target="_blank"
            rel="noreferrer"
          >
            打开原始文件 ↗
          </a>
        ) : target.kind === "url" && target.url ? (
          <a href={target.url} target="_blank" rel="noreferrer">
            打开网页 ↗
          </a>
        ) : target.kind === "obsidian" ? (
          target.item_id ? (
            <button
              className="text-button citation-open-button"
              type="button"
              onClick={() => onOpenObsidian(target.item_id as string)}
            >
              在 Obsidian 打开 ↗
            </button>
          ) : (
            <span>请在 Obsidian 中查看正文</span>
          )
        ) : (
          <span>仅保留证据摘录</span>
        )}
      </div>
    </article>
  );
}

function EvidenceBadge({ evidence }: { evidence: EvidenceAssessment | null }) {
  if (!evidence) {
    return null;
  }
  const label =
    evidence.status === "sufficient"
      ? "证据充分"
      : evidence.status === "low_confidence"
        ? "证据信心较低"
        : "没有命中证据";
  return <span className={"evidence-badge evidence-" + evidence.status}>{label}</span>;
}

export function SearchPage() {
  const [query, setQuery] = useState("");
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const [chatClaims, setChatClaims] = useState<ChatClaim[]>([]);
  const [chatCitations, setChatCitations] = useState<Citation[]>([]);
  const [chatEvidence, setChatEvidence] = useState<EvidenceAssessment | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  const [busy, setBusy] = useState<"search" | "chat" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const openCitationInObsidian = async (itemId: string) => {
    try {
      const { uri } = await openObsidian(itemId);
      window.location.href = uri;
    } catch (reason) {
      setError(messageFor(reason));
    }
  };

  const submitSearch = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim()) {
      setError("请输入搜索问题或关键词");
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy("search");
    setError(null);
    setAnswer(null);
    setChatClaims([]);
    setChatCitations([]);
    try {
      setSearchResult(await searchKnowledge(query.trim(), { signal: controller.signal }));
    } catch (reason) {
      if (!(reason instanceof ApiError && reason.code === "request_cancelled")) {
        setError(messageFor(reason));
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
      }
      setBusy(null);
    }
  };

  const ask = async () => {
    if (!query.trim()) {
      setError("请输入要回答的问题");
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy("chat");
    setError(null);
    setAnswer(null);
    setChatClaims([]);
    setChatCitations([]);
    setChatEvidence(null);
    try {
      await streamChat(query.trim(), {
        signal: controller.signal,
        onEvent: (streamEvent) => {
          if (streamEvent.event === "meta") {
            setChatEvidence(streamEvent.data.evidence);
          } else if (streamEvent.event === "delta") {
            setChatClaims((current) => [...current, streamEvent.data]);
          } else if (streamEvent.event === "citations") {
            setChatCitations(streamEvent.data.citations);
          } else if (streamEvent.event === "done") {
            setAnswer(streamEvent.data.answer ?? null);
          }
        },
      });
    } catch (reason) {
      if (!(reason instanceof ApiError && reason.code === "request_cancelled")) {
        setError(messageFor(reason));
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
      }
      setBusy(null);
    }
  };

  return (
    <section className="stage-page search-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">Hybrid Retrieval · Evidence First</span>
          <h1>搜索与问答</h1>
          <p>先检索当前已发布版本，再决定是否基于证据回答。</p>
        </div>
        <span className="local-badge">SQLite + Qdrant Local</span>
      </div>

      <form className="search-form" onSubmit={(event) => void submitSearch(event)}>
        <label htmlFor="knowledge-search">搜索问题或关键词</label>
        <div className="search-input-row">
          <input
            id="knowledge-search"
            aria-label="搜索问题或关键词"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：当前版本如何做证据校验？"
          />
          <button className="ghost-button" disabled={busy !== null} type="submit">
            {busy === "search" ? "检索中…" : "搜索"}
          </button>
          <button className="primary-button" disabled={busy !== null} type="button" onClick={() => void ask()}>
            {busy === "chat" ? "回答中…" : "基于证据回答"}
          </button>
        </div>
        <span className="search-form-note">未达到证据阈值时会明确拒答，不调用答案模型。</span>
      </form>

      {error ? <div className="inline-error" role="alert">{error}</div> : null}

      {chatClaims.length > 0 || answer ? (
        <section className="answer-panel">
          <div className="section-heading compact">
            <div><span className="eyebrow">受证据约束的回答</span><h2>回答</h2></div>
            <EvidenceBadge evidence={chatEvidence} />
          </div>
          <div className="answer-copy">
            {chatClaims.map((claim, index) => (
              <p key={String(index)}>
                {claim.text}
                <span className="claim-citations">
                  {claim.citation_ids.map((citationId) => (
                    <span className="citation-chip" key={citationId}>{citationId}</span>
                  ))}
                </span>
              </p>
            ))}
          </div>
          {chatCitations.length > 0 ? (
            <div className="citation-grid">
              {chatCitations.map((citation) => (
                <CitationCard
                  citation={citation}
                  key={citation.citation_id}
                  onOpenObsidian={(itemId) => void openCitationInObsidian(itemId)}
                />
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {searchResult ? (
        <section className="search-results">
          <div className="section-heading compact">
            <div><span className="eyebrow">检索结果</span><h2>当前知识证据</h2></div>
            <EvidenceBadge evidence={searchResult.evidence} />
          </div>
          {searchResult.results.length === 0 ? (
            <div className="empty-panel"><span className="empty-panel-icon">⌕</span><strong>没有找到当前版本证据</strong><p>{searchResult.evidence.reason}</p></div>
          ) : (
            <div className="citation-grid">
              {searchResult.results.map((result) => (
                <CitationCard
                  citation={result.citation}
                  key={result.citation.citation_id}
                  onOpenObsidian={(itemId) => void openCitationInObsidian(itemId)}
                />
              ))}
            </div>
          )}
        </section>
      ) : (
        <div className="empty-panel search-empty"><span className="empty-panel-icon">⌕</span><strong>从一个问题开始</strong><p>搜索会返回可追溯的当前版本证据，问答会把每个事实绑定到 citation。</p></div>
      )}
    </section>
  );
}
