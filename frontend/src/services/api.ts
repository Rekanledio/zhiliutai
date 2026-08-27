export type HealthState =
  | "healthy"
  | "degraded"
  | "not_configured"
  | "configured"
  | "unavailable";

export interface HealthComponent {
  key: string;
  label: string;
  state: HealthState;
  detail: string;
  latency_ms?: number | null;
}

export interface HealthResponse {
  status: "healthy" | "degraded";
  checked_at: string;
  components: HealthComponent[];
}

export interface DashboardStats {
  knowledge_count: number;
  today_added: number;
  pending_review: number;
  processing: number;
}

export interface DashboardResponse {
  greeting: string;
  date_label: string;
  stats: DashboardStats;
  health: HealthResponse;
  pending_reviews: Array<Record<string, string>>;
  recent_items: Array<Record<string, string>>;
  processing_jobs: Array<Record<string, string>>;
}

export interface KnowledgeItem {
  id: string;
  title: string;
  source_type: string;
  status: string;
  content_hash: string;
  body?: string | null;
  summary?: string | null;
  suggested_tags: string[];
  version_no?: number | null;
  note_relative_path?: string | null;
  sync_state?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProcessingJob {
  id: string;
  kind: string;
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stage: string;
  progress: number;
  retry_count: number;
  max_retries: number;
  error?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  heartbeat_at?: string | null;
  created_at: string;
}

export interface ObsidianStatus {
  configured: boolean;
  watcher_running: boolean;
  managed_directory?: string | null;
  last_heartbeat_at?: string | null;
  last_error?: string | null;
}

export interface CitationLocator {
  kind: "pdf" | "docx" | "webpage" | "obsidian" | "none";
  page?: number;
  page_label?: string;
  element?: string;
  heading_level?: number;
  heading_path?: string[];
  paragraph?: number;
  table?: number;
  row?: number;
  url?: string;
  path?: string;
}

export interface CitationTarget {
  kind: "artifact" | "url" | "obsidian" | "none";
  artifact_id?: string;
  item_id?: string;
  page?: number;
  url?: string;
}

export interface RetrievalInfo {
  matched_by: string[];
  fts_rank?: number;
  vector_rank?: number;
  fts_score?: number;
  vector_score?: number;
  rrf_score: number;
  rerank_score?: number;
}

export interface Citation {
  citation_id: string;
  chunk_id: string;
  knowledge_item_id: string;
  content_version_id: string;
  item_title: string;
  version_no: number;
  source_type: string;
  excerpt: string;
  chunk_content_hash: string;
  locator_status: "exact" | "fallback" | "unavailable";
  locator: CitationLocator;
  target: CitationTarget;
  retrieval: RetrievalInfo;
}

export interface EvidenceAssessment {
  status: "none" | "low_confidence" | "sufficient";
  reason: string;
}

export interface RetrievalDiagnostics {
  original_query: string;
  normalized_query: string;
  fts_query?: string;
  fts_available: boolean;
  vector_available: boolean;
  degraded: boolean;
  channel_errors: Record<string, string>;
  reranker_available?: boolean;
}

export interface SearchResult {
  chunk_id: string;
  knowledge_item_id: string;
  content_version_id: string;
  item_title: string;
  version_no: number;
  source_type: string;
  excerpt: string;
  citation: Citation;
}

export interface SearchResponse {
  query: string;
  normalized_query: string;
  results: SearchResult[];
  evidence: EvidenceAssessment;
  diagnostics: RetrievalDiagnostics;
  searched_at: string;
}

export interface ChatClaim {
  text: string;
  citation_ids: string[];
}

export interface ChatMetaEvent {
  query: string;
  normalized_query: string;
  evidence: EvidenceAssessment;
  diagnostics: RetrievalDiagnostics;
  rewrite_query?: string;
  rewrite_status: string;
  refusal?: string;
}

export interface ChatCitationsEvent {
  citations: Citation[];
}

export interface ChatDoneEvent {
  answer?: string;
  conflicts: string[];
  model_run_id?: string;
}

export type ChatStreamEvent =
  | { event: "meta"; data: ChatMetaEvent }
  | { event: "delta"; data: ChatClaim }
  | { event: "citations"; data: ChatCitationsEvent }
  | { event: "done"; data: ChatDoneEvent };

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null,
    public readonly code: string,
    public readonly requestId: string | null,
  ) {
    super(message);
  }
}

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";

function clientRequestId(): string {
  const raw =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : "web-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  return raw.replace(/[^A-Za-z0-9._-]/g, "").slice(0, 80);
}

function displayError(message: string, requestId: string | null): string {
  return requestId ? message + "（请求 ID：" + requestId + "）" : message;
}

async function apiErrorFromResponse(response: Response, fallbackMessage: string): Promise<ApiError> {
  const requestId = response.headers?.get?.("X-Request-ID") ?? null;
  let code = "http_error";
  let message = fallbackMessage;
  try {
    const payload = (await response.json()) as {
      error?: { code?: string; message?: string; request_id?: string };
    };
    code = payload.error?.code ?? code;
    message = payload.error?.message ?? message;
    const serverRequestId = payload.error?.request_id ?? requestId;
    return new ApiError(displayError(message, serverRequestId), response.status, code, serverRequestId);
  } catch {
    return new ApiError(displayError(message, requestId), response.status, code, requestId);
  }
}

export async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 10_000,
): Promise<T> {
  const controller = new AbortController();
  const externalSignal = init.signal;
  const cancel = () => controller.abort(externalSignal?.reason);
  externalSignal?.addEventListener("abort", cancel, { once: true });
  const timeout = window.setTimeout(() => controller.abort("timeout"), timeoutMs);
  try {
    const response = await fetch(apiBase + path, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        "X-Request-ID": clientRequestId(),
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
    const requestId = response.headers?.get?.("X-Request-ID") ?? null;
    if (!response.ok) {
      let code = "http_error";
      let message = "API 请求失败：" + response.status;
      try {
        const payload = (await response.json()) as {
          error?: { code?: string; message?: string; request_id?: string };
        };
        code = payload.error?.code ?? code;
        message = payload.error?.message ?? message;
        throw new ApiError(
          displayError(message, payload.error?.request_id ?? requestId),
          response.status,
          code,
          payload.error?.request_id ?? requestId,
        );
      } catch (error) {
        if (error instanceof ApiError) {
          throw error;
        }
        throw new ApiError(displayError(message, requestId), response.status, code, requestId);
      }
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (controller.signal.aborted) {
      const timedOut = controller.signal.reason === "timeout";
      throw new ApiError(
        timedOut ? "请求超时，请稍后重试" : "请求已取消",
        null,
        timedOut ? "request_timeout" : "request_cancelled",
        null,
      );
    }
    throw new ApiError("无法连接本机 API", null, "network_error", null);
  } finally {
    window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", cancel);
  }
}

export function searchKnowledge(
  query: string,
  options: {
    limit?: number;
    rewrite?: "auto" | "off";
    sourceTypes?: string[];
    signal?: AbortSignal;
  } = {},
): Promise<SearchResponse> {
  return requestJson<SearchResponse>("/api/search", {
    method: "POST",
    body: JSON.stringify({
      query,
      limit: options.limit ?? 6,
      rewrite: options.rewrite ?? "off",
      source_types: options.sourceTypes,
    }),
    signal: options.signal,
  });
}

export async function streamChat(
  query: string,
  options: {
    limit?: number;
    rewrite?: "auto" | "off";
    signal?: AbortSignal;
    timeoutMs?: number;
    onEvent: (event: ChatStreamEvent) => void;
  },
): Promise<void> {
  const controller = new AbortController();
  const externalSignal = options.signal;
  const cancel = () => controller.abort(externalSignal?.reason);
  externalSignal?.addEventListener("abort", cancel, { once: true });
  const timeout = window.setTimeout(
    () => controller.abort("timeout"),
    options.timeoutMs ?? 60_000,
  );
  try {
    const response = await fetch(apiBase + "/api/chat/stream", {
      method: "POST",
      body: JSON.stringify({
        query,
        limit: options.limit ?? 6,
        rewrite: options.rewrite ?? "off",
      }),
      signal: controller.signal,
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": clientRequestId(),
      },
    });
    if (!response.ok) {
      throw await apiErrorFromResponse(response, "问答 API 请求失败：" + response.status);
    }
    if (!response.body) {
      throw new ApiError("问答流没有返回内容", response.status, "empty_stream", null);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const emitFrame = (frame: string) => {
      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      }
      if (!dataLines.length) {
        return;
      }
      let data: unknown;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch {
        throw new ApiError("问答流格式无效", response.status, "invalid_stream", null);
      }
      if (
        eventName === "meta" ||
        eventName === "delta" ||
        eventName === "citations" ||
        eventName === "done"
      ) {
        options.onEvent({ event: eventName, data } as ChatStreamEvent);
      }
    };

    while (true) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, { stream: !next.done });
      let separator = buffer.indexOf("\n\n");
      while (separator >= 0) {
        emitFrame(buffer.slice(0, separator));
        buffer = buffer.slice(separator + 2);
        separator = buffer.indexOf("\n\n");
      }
      if (next.done) {
        if (buffer.trim()) {
          emitFrame(buffer);
        }
        break;
      }
    }
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (controller.signal.aborted) {
      const timedOut = controller.signal.reason === "timeout";
      throw new ApiError(
        timedOut ? "问答请求超时，请稍后重试" : "请求已取消",
        null,
        timedOut ? "request_timeout" : "request_cancelled",
        null,
      );
    }
    throw new ApiError("无法连接本机 API", null, "network_error", null);
  } finally {
    window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", cancel);
  }
}

export function getDashboard(signal?: AbortSignal): Promise<DashboardResponse> {
  return requestJson<DashboardResponse>("/api/dashboard", { signal });
}

export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/api/health", { signal });
}

export function submitText(
  content: string,
  sourceType: "text" | "markdown",
  signal?: AbortSignal,
): Promise<{ item_id: string; job_id: string; deduplicated: boolean }> {
  return requestJson("/api/sources/text", {
    method: "POST",
    body: JSON.stringify({ content, source_type: sourceType }),
    signal,
  });
}

export function getJobs(signal?: AbortSignal): Promise<ProcessingJob[]> {
  return requestJson("/api/jobs", { signal });
}

export function getJob(id: string, signal?: AbortSignal): Promise<ProcessingJob> {
  return requestJson("/api/jobs/" + id, { signal });
}

export function retryJob(id: string): Promise<ProcessingJob> {
  return requestJson("/api/jobs/" + id + "/retry", { method: "POST" });
}

export function getItems(status?: string, signal?: AbortSignal): Promise<KnowledgeItem[]> {
  const query = status ? "?status=" + encodeURIComponent(status) : "";
  return requestJson("/api/items" + query, { signal });
}

export function getItem(id: string, signal?: AbortSignal): Promise<KnowledgeItem> {
  return requestJson("/api/items/" + id, { signal });
}

export function reviewItem(id: string): Promise<KnowledgeItem> {
  return requestJson("/api/items/" + id + "/review", {
    method: "POST",
    body: JSON.stringify({ approved: true }),
  });
}

export function publishItem(id: string): Promise<KnowledgeItem> {
  return requestJson("/api/items/" + id + "/publish", { method: "POST" });
}

export function getObsidianStatus(signal?: AbortSignal): Promise<ObsidianStatus> {
  return requestJson("/api/obsidian/status", { signal });
}

export function rescanObsidian(): Promise<Record<string, number>> {
  return requestJson("/api/obsidian/rescan", { method: "POST" });
}

export function openObsidian(id: string): Promise<{ uri: string }> {
  return requestJson("/api/obsidian/open/" + id, { method: "POST" });
}
