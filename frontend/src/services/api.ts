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
  current_content_version_id?: string | null;
  pending_content_version_id?: string | null;
  has_pending_review?: boolean;
  source_metadata?: Record<string, unknown> | null;
  body?: string | null;
  summary?: string | null;
  suggested_tags: string[];
  version_no?: number | null;
  note_relative_path?: string | null;
  sync_state?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CollectionItem {
  id: string;
  title: string;
  source_type: string;
  version_no: number;
  suggested_tags: string[];
}

export interface CollectionSummary {
  id: string;
  name: string;
  description?: string | null;
  item_count: number;
  moc_enabled: boolean;
}

export interface Collection extends CollectionSummary {
  items: CollectionItem[];
  related_tags: string[];
  moc_status: "not_enabled";
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

export type SettingsHealthState = HealthState;

export interface ProviderSettings {
  capability: "chat" | "embedding" | "asr" | "vision" | "reranker";
  provider_kind: "openai-compatible" | "fastembed";
  configured: boolean;
  credential_configured: boolean;
  model: string | null;
}

export interface SettingsResponse {
  local_only: boolean;
  bind_host: "127.0.0.1" | "loopback" | "non_loopback";
  vault: {
    configured: boolean;
    managed_directory: string | null;
    watcher_running: boolean;
    sync_state: "watching" | "stopped" | "degraded" | "not_configured";
  };
  providers: {
    chat: ProviderSettings;
    embedding: ProviderSettings;
    asr: ProviderSettings;
    vision: ProviderSettings;
    reranker: ProviderSettings;
  };
  retrieval: {
    rag_query_max_chars: number;
    rrf_k: number;
    fts_limit: number;
    vector_limit: number;
    threshold: number;
    confident_rank: number;
    rerank_limit: number;
  };
  chunking: {
    strategy: "paragraph_then_fixed_width";
    max_chars: number;
  };
  video: {
    retention_policy: "permanent" | "until_expiry" | "delete_after_processing";
    retention_days: number;
    max_bytes: number;
    max_duration_seconds: number;
    ffmpeg_state: SettingsHealthState;
  };
  maintenance: {
    backup_available: boolean;
    rescan_available: boolean;
    rebuild_available: boolean;
    configuration_hint: string;
    restore_note: string;
  };
}

export interface SettingsRescanResponse {
  changed: number;
  renamed: number;
  missing: number;
  conflicts: number;
  invalid: number;
  deferred: number;
}

export interface SettingsRebuildResponse {
  published_items: number;
  chunks: number;
}

export interface SettingsBackupResponse {
  archive_id: string;
  created_at: string;
  sha256: string;
  config_key: "BACKUP_ROOT";
}

export interface CitationLocator {
  kind:
    | "pdf"
    | "docx"
    | "webpage"
    | "obsidian"
    | "video"
    | "video_chapter"
    | "video_keyframe"
    | "none";
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
  start_ms?: number;
  end_ms?: number;
  language?: string;
  event_type?: "scene" | "slide" | "code" | "ui" | "speaker" | "other";
  keyframe_ids?: string[];
}

export interface CitationTarget {
  kind: "artifact" | "url" | "obsidian" | "none";
  artifact_id?: string;
  item_id?: string;
  page?: number;
  url?: string;
  start_ms?: number;
  end_ms?: number;
  keyframe_id?: string;
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

export interface VideoCitationSegment {
  start_ms: number;
  end_ms: number;
  text: string;
  language?: string | null;
}

export interface VideoCitationPreview {
  kind: "transcript" | "keyframe";
  artifact_id: string;
  start_ms: number;
  end_ms: number;
  text?: string;
  language?: string | null;
  segments?: VideoCitationSegment[];
  keyframe_id?: string;
  media_type?: string;
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
    sourceTypes?: string[];
    signal?: AbortSignal;
  } = {},
): Promise<SearchResponse> {
  return requestJson<SearchResponse>("/api/search", {
    method: "POST",
    body: JSON.stringify({
      query,
      limit: options.limit ?? 6,
      source_types: options.sourceTypes,
    }),
    signal: options.signal,
  });
}

function openTarget(target: string, unavailableMessage: string, code: string): void {
  if (typeof window.open !== "function") {
    throw new ApiError(unavailableMessage, null, code, null);
  }
  const opened = window.open(target, "_blank", "noopener,noreferrer");
  if (!opened) {
    throw new ApiError(unavailableMessage, null, code, null);
  }
}

export async function openArtifact(
  artifactId: string,
  page?: number,
  options: { startMs?: number; endMs?: number; keyframeId?: string } = {},
): Promise<void> {
  if (!/^[A-Za-z0-9-]{1,80}$/.test(artifactId)) {
    throw new ApiError("来源文件链接无效", null, "invalid_artifact_target", null);
  }
  if (page !== undefined && (!Number.isInteger(page) || page < 1)) {
    throw new ApiError("来源页码无效", null, "invalid_artifact_target", null);
  }
  const path = "/api/artifacts/" + encodeURIComponent(artifactId);
  try {
    const response = await fetch(apiBase + path, {
      method: "HEAD",
      headers: {
        Accept: "application/octet-stream",
        "X-Request-ID": clientRequestId(),
      },
    });
    if (!response.ok) {
      throw await apiErrorFromResponse(response, "来源文件不可访问");
    }
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError("来源文件不可访问", null, "artifact_unavailable", null);
  }
  const fragment =
    page !== undefined
      ? "#page=" + encodeURIComponent(String(page))
      : options.startMs !== undefined
        ? "#t=" +
          encodeURIComponent(
            String(options.startMs / 1000) +
              (options.endMs === undefined ? "" : "," + String(options.endMs / 1000)),
          ) +
          (options.keyframeId
            ? "&keyframe=" + encodeURIComponent(options.keyframeId)
            : "")
        : "";
  openTarget(
    apiBase + path + fragment,
    "浏览器阻止打开来源文件",
    "target_blocked",
  );
}

export async function getVideoCitation(
  artifactId: string,
  options: { startMs?: number; endMs?: number; keyframeId?: string } = {},
): Promise<VideoCitationPreview> {
  if (!/^[A-Za-z0-9-]{1,80}$/.test(artifactId)) {
    throw new ApiError("来源文件链接无效", null, "invalid_artifact_target", null);
  }
  if (
    (options.startMs !== undefined &&
      (!Number.isInteger(options.startMs) || options.startMs < 0)) ||
    (options.endMs !== undefined &&
      (!Number.isInteger(options.endMs) || options.endMs <= 0)) ||
    (options.startMs !== undefined &&
      options.endMs !== undefined &&
      options.startMs >= options.endMs) ||
    (options.keyframeId !== undefined &&
      (options.keyframeId.length < 1 ||
        options.keyframeId.length > 200 ||
        /[\u0000-\u001f\u007f]/.test(options.keyframeId)))
  ) {
    throw new ApiError("视频时间戳无效", null, "invalid_video_locator", null);
  }
  const query = new URLSearchParams();
  if (options.startMs !== undefined) {
    query.set("start_ms", String(options.startMs));
  }
  if (options.endMs !== undefined) {
    query.set("end_ms", String(options.endMs));
  }
  if (options.keyframeId !== undefined) {
    query.set("keyframe_id", options.keyframeId);
  }
  const suffix = query.toString() ? "?" + query.toString() : "";
  const preview = await requestJson<VideoCitationPreview>(
    "/api/artifacts/" + encodeURIComponent(artifactId) + "/locator" + suffix,
  );
  if (
    !preview ||
    (preview.kind !== "transcript" && preview.kind !== "keyframe") ||
    preview.artifact_id !== artifactId ||
    !Number.isInteger(preview.start_ms) ||
    !Number.isInteger(preview.end_ms) ||
    preview.start_ms < 0 ||
    preview.start_ms >= preview.end_ms
  ) {
    throw new ApiError("视频定位响应无效", null, "invalid_video_locator", null);
  }
  return preview;
}

export function openExternalUrl(url: string): void {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new ApiError("网页来源链接无效", null, "invalid_url_target", null);
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    Array.from(parsed.searchParams.keys()).some((key) =>
      ["api_key", "apikey", "key", "access_token", "authorization", "password", "secret", "token"].includes(
        key.toLowerCase().replaceAll("-", "_"),
      ),
    ) ||
    Array.from(new URLSearchParams(parsed.hash.replace(/^#/, "")).keys()).some((key) =>
      ["api_key", "apikey", "key", "access_token", "authorization", "password", "secret", "token"].includes(
        key.toLowerCase().replaceAll("-", "_"),
      ),
    ) ||
    /\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9._-]{6,}\b/i.test(url)
  ) {
    throw new ApiError("网页来源链接无效", null, "invalid_url_target", null);
  }
  openTarget(parsed.toString(), "浏览器阻止打开网页来源", "target_blocked");
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
    let streamPhase: "start" | "meta" | "delta" | "citations" | "done" = "start";
    let sawDone = false;

    const validateStreamOrder = (eventName: string) => {
      const valid =
        (eventName === "meta" && streamPhase === "start") ||
        (eventName === "delta" && (streamPhase === "meta" || streamPhase === "delta")) ||
        (eventName === "citations" && (streamPhase === "meta" || streamPhase === "delta")) ||
        (eventName === "done" && streamPhase === "citations");
      if (!valid) {
        throw new ApiError("问答流顺序无效", response.status, "invalid_stream_order", null);
      }
      streamPhase = eventName as typeof streamPhase;
      sawDone = eventName === "done";
    };

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
        validateStreamOrder(eventName);
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
    if (!sawDone) {
      throw new ApiError("问答流不完整", response.status, "invalid_stream_order", null);
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

export function getSettings(signal?: AbortSignal): Promise<SettingsResponse> {
  return requestJson<SettingsResponse>("/api/settings", { signal });
}

export function rescanSettings(): Promise<SettingsRescanResponse> {
  return requestJson<SettingsRescanResponse>("/api/settings/rescan", { method: "POST" }, 60_000);
}

export function rebuildDerivedState(): Promise<SettingsRebuildResponse> {
  return requestJson<SettingsRebuildResponse>("/api/settings/rebuild", { method: "POST" }, 120_000);
}

export function createBackup(): Promise<SettingsBackupResponse> {
  return requestJson<SettingsBackupResponse>("/api/settings/backup", { method: "POST" }, 120_000);
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

export function submitVideo(
  url: string,
  options: {
    title?: string;
    language?: string;
    enableVision?: boolean;
    idempotencyKey?: string;
    signal?: AbortSignal;
  } = {},
): Promise<{ item_id: string; job_id: string; deduplicated: boolean }> {
  return requestJson("/api/sources/video", {
    method: "POST",
    body: JSON.stringify({
      url,
      title: options.title || undefined,
      language: options.language || undefined,
      enable_vision: options.enableVision ?? false,
      idempotency_key: options.idempotencyKey || undefined,
    }),
    signal: options.signal,
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

export function cancelJob(id: string): Promise<ProcessingJob> {
  return requestJson("/api/jobs/" + id + "/cancel", { method: "POST" });
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

function collectionPath(id: string): string {
  return "/api/collections/" + encodeURIComponent(id);
}

export function getCollections(signal?: AbortSignal): Promise<CollectionSummary[]> {
  return requestJson("/api/collections", { signal });
}

export function getCollection(id: string, signal?: AbortSignal): Promise<Collection> {
  return requestJson(collectionPath(id), { signal });
}

export function createCollection(
  name: string,
  description?: string,
): Promise<Collection> {
  return requestJson("/api/collections", {
    method: "POST",
    body: JSON.stringify({ name, description: description || null }),
  });
}

export function updateCollection(
  id: string,
  updates: { name?: string; description?: string | null },
): Promise<Collection> {
  return requestJson(collectionPath(id), {
    method: "PATCH",
    body: JSON.stringify(updates),
  });
}

export function deleteCollection(id: string): Promise<void> {
  return requestJson(collectionPath(id), { method: "DELETE" });
}

export function addCollectionItem(
  collectionId: string,
  itemId: string,
): Promise<Collection> {
  return requestJson(
    collectionPath(collectionId) + "/items/" + encodeURIComponent(itemId),
    { method: "POST" },
  );
}

export function removeCollectionItem(
  collectionId: string,
  itemId: string,
): Promise<Collection> {
  return requestJson(
    collectionPath(collectionId) + "/items/" + encodeURIComponent(itemId),
    { method: "DELETE" },
  );
}
