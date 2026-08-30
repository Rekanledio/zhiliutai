import { expect, test } from "@playwright/test";

const now = "2026-08-28T00:00:00+08:00";
const itemId = "11111111-1111-4111-8111-111111111111";
const versionId = "22222222-2222-4222-8222-222222222222";
const jobId = "33333333-3333-4333-8333-333333333333";
const collectionId = "66666666-6666-4666-8666-666666666666";

const itemBody = "SQLite 是合成浏览器闭环的版本权威。";

function health() {
  return {
    status: "healthy",
    checked_at: now,
    components: [
      { key: "api", label: "FastAPI", state: "healthy", detail: "服务进程正常" },
      { key: "sqlite", label: "SQLite", state: "healthy", detail: "本地数据库可用" },
      { key: "qdrant", label: "Qdrant Local", state: "healthy", detail: "本地客户端可用" },
      { key: "artifact_storage", label: "Artifact Storage", state: "healthy", detail: "目录可读写" },
      { key: "obsidian", label: "Obsidian Vault", state: "healthy", detail: "Vault 可访问" },
      { key: "obsidian_watcher", label: "Obsidian Watcher", state: "healthy", detail: "监听中" },
      { key: "model_providers", label: "Model Providers", state: "configured", detail: "确定性测试 provider" },
      { key: "ffmpeg", label: "FFmpeg", state: "not_configured", detail: "仅影响视频能力" },
    ],
  };
}

function item(status: "pending_review" | "reviewed" | "published") {
  return {
    id: itemId,
    title: "合成 E2E 知识",
    source_type: "markdown",
    status,
    content_hash: "a".repeat(64),
    current_content_version_id: status === "published" ? versionId : null,
    pending_content_version_id: status === "published" ? null : versionId,
    has_pending_review: status !== "published",
    body: itemBody,
    summary: "确定性浏览器测试摘要",
    suggested_tags: ["测试"],
    suggested_collections: ["合成阅读合集"],
    confirmed_tags: status === "published" ? ["测试"] : [],
    collections: status === "published" ? ["合成阅读合集"] : [],
    source_metadata: {},
    version_no: 1,
    note_relative_path: "知流台/合成-e2e.md",
    sync_state: status === "published" ? "synced" : null,
    created_at: now,
    updated_at: now,
  };
}

function dashboard(status: "pending_review" | "reviewed" | "published") {
  const pending = status === "pending_review";
  return {
    greeting: "下午好",
    date_label: "8 月 28 日 · 星期五",
    stats: {
      knowledge_count: status === "published" ? 1 : 0,
      today_added: status === "published" ? 1 : 0,
      pending_review: pending ? 1 : 0,
      processing: 0,
    },
    health: health(),
    pending_reviews: pending
      ? [
          {
            id: itemId,
            title: "合成 E2E 知识",
            source_type: "markdown",
            status: "pending_review",
            updated_at: now,
          },
        ]
      : [],
    recent_items:
      status === "published"
        ? [{ id: itemId, title: "合成 E2E 知识", source_type: "markdown", status, updated_at: now }]
        : [],
    processing_jobs: [],
  };
}

function collectionSummary() {
  return {
    id: collectionId,
    name: "合成阅读合集",
    description: "只读合成说明",
    item_count: 1,
    moc_enabled: false,
  };
}

function collectionDetail(hasMember: boolean) {
  return {
    ...collectionSummary(),
    item_count: hasMember ? 1 : 0,
    items: hasMember
      ? [
          {
            id: itemId,
            title: "合成 E2E 知识",
            source_type: "markdown",
            version_no: 1,
            suggested_tags: ["测试"],
            confirmed_tags: ["测试"],
          },
        ]
      : [],
    related_tags: hasMember ? ["测试"] : [],
    moc_status: "not_enabled" as const,
  };
}

function settingsResponse() {
  return {
    local_only: true,
    bind_host: "127.0.0.1",
    vault: {
      configured: true,
      managed_directory: "知流台",
      watcher_running: true,
      sync_state: "watching",
    },
    providers: {
      chat: {
        capability: "chat",
        provider_kind: "openai-compatible",
        configured: true,
        credential_configured: true,
        model: "synthetic-chat",
      },
      embedding: {
        capability: "embedding",
        provider_kind: "fastembed",
        configured: true,
        credential_configured: false,
        model: "synthetic-embedding",
      },
      asr: {
        capability: "asr",
        provider_kind: "openai-compatible",
        configured: false,
        credential_configured: false,
        model: null,
      },
      vision: {
        capability: "vision",
        provider_kind: "openai-compatible",
        configured: false,
        credential_configured: false,
        model: null,
      },
      reranker: {
        capability: "reranker",
        provider_kind: "openai-compatible",
        configured: false,
        credential_configured: false,
        model: null,
      },
    },
    retrieval: {
      rag_query_max_chars: 2000,
      rrf_k: 60,
      fts_limit: 30,
      vector_limit: 30,
      threshold: 0.35,
      confident_rank: 3,
      rerank_limit: 20,
    },
    chunking: {
      strategy: "paragraph_then_fixed_width",
      max_chars: 800,
    },
    video: {
      retention_policy: "delete_after_processing",
      retention_days: 7,
      max_bytes: 500000000,
      max_duration_seconds: 14400,
      ffmpeg_state: "not_configured",
    },
    maintenance: {
      backup_available: true,
      rescan_available: true,
      rebuild_available: true,
      configuration_hint: "配置通过项目根目录 .env，重启后生效；API Key 仅在后端秘密配置中使用。",
      restore_note: "恢复必须先停止服务，再按文档化离线 CLI 执行；设置页不提供在线恢复。",
    },
  };
}

function citation() {
  return {
    citation_id: "C1",
    chunk_id: "44444444-4444-4444-8444-444444444444",
    knowledge_item_id: itemId,
    content_version_id: versionId,
    item_title: "合成 E2E 知识",
    version_no: 1,
    source_type: "markdown",
    excerpt: itemBody,
    chunk_content_hash: "b".repeat(64),
    locator_status: "exact",
    locator: { kind: "obsidian", path: "知流台/合成-e2e.md" },
    target: { kind: "obsidian", item_id: itemId },
    retrieval: {
      matched_by: ["fts", "vector"],
      fts_rank: 1,
      vector_rank: 1,
      rrf_score: 1,
    },
  };
}

function searchResponse() {
  const currentCitation = citation();
  return {
    query: "SQLite",
    normalized_query: "SQLite",
    results: [
      {
        chunk_id: currentCitation.chunk_id,
        knowledge_item_id: itemId,
        content_version_id: versionId,
        item_title: "合成 E2E 知识",
        version_no: 1,
        source_type: "markdown",
        excerpt: itemBody,
        citation: currentCitation,
      },
    ],
    evidence: { status: "sufficient", reason: "命中当前版本证据" },
    diagnostics: {
      original_query: "SQLite",
      normalized_query: "SQLite",
      fts_query: "SQLite",
      fts_available: true,
      vector_available: true,
      degraded: false,
      channel_errors: {},
      reranker_available: false,
    },
    searched_at: now,
  };
}

function chatStream() {
  const diagnostics = searchResponse().diagnostics;
  const evidence = { status: "sufficient", reason: "命中当前版本证据" };
  return [
    "event: meta",
    `data: ${JSON.stringify({
      query: "SQLite",
      normalized_query: "SQLite",
      evidence,
      diagnostics,
      rewrite_status: "off",
    })}`,
    "",
    "event: delta",
    `data: ${JSON.stringify({
      text: "SQLite 是当前回答使用的权威校验来源。",
      citation_ids: ["C1"],
    })}`,
    "",
    "event: citations",
    `data: ${JSON.stringify({ citations: [citation()] })}`,
    "",
    "event: done",
    `data: ${JSON.stringify({
      answer: "SQLite 是当前回答使用的权威校验来源。",
      conflicts: [],
      model_run_id: "55555555-5555-4555-8555-555555555555",
    })}`,
    "",
  ].join("\n");
}

test("synthetic review publish search answer and citation flow", async ({ page }) => {
  let status: "pending_review" | "reviewed" | "published" = "pending_review";
  let submitted = false;
  const observed: string[] = [];

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    observed.push(`${request.method()} ${path}`);

    if (request.method() === "GET" && path === "/api/dashboard") {
      await route.fulfill({ json: dashboard(status) });
      return;
    }
    if (request.method() === "GET" && path === "/api/items") {
      await route.fulfill({ json: submitted && status !== "published" ? [item(status)] : [] });
      return;
    }
    if (request.method() === "POST" && path === "/api/sources/text") {
      const body = request.postDataJSON() as { content?: string; source_type?: string };
      expect(body.content).toContain("SQLite");
      expect(body.source_type).toBe("markdown");
      submitted = true;
      await route.fulfill({
        status: 202,
        json: { item_id: itemId, job_id: jobId, deduplicated: false },
      });
      return;
    }
    if (request.method() === "GET" && path === `/api/jobs/${jobId}`) {
      await route.fulfill({
        json: {
          id: jobId,
          kind: "ingest_text",
          state: "succeeded",
          stage: "completed",
          progress: 1,
          retry_count: 0,
          max_retries: 3,
          error: null,
          result: { item_id: itemId },
          heartbeat_at: now,
          created_at: now,
        },
      });
      return;
    }
    if (request.method() === "GET" && path === `/api/items/${itemId}`) {
      await route.fulfill({ json: item(status) });
      return;
    }
    if (request.method() === "POST" && path === `/api/items/${itemId}/review`) {
      status = "reviewed";
      await route.fulfill({ json: item(status) });
      return;
    }
    if (request.method() === "POST" && path === `/api/items/${itemId}/publish`) {
      status = "published";
      await route.fulfill({ json: item(status) });
      return;
    }
    if (request.method() === "POST" && path === "/api/search") {
      await route.fulfill({ json: searchResponse() });
      return;
    }
    if (request.method() === "POST" && path === "/api/chat/stream") {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream; charset=utf-8" },
        body: chatStream(),
      });
      return;
    }
    throw new Error(`Unhandled synthetic API request: ${request.method()} ${path}`);
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "下午好，先整理一点点" })).toBeVisible();

  await page.getByRole("button", { name: "收件箱" }).click();
  await expect(page.getByRole("heading", { name: "收件箱" })).toBeVisible();
  await page.getByRole("button", { name: "Markdown" }).click();
  await page.getByLabel("知识内容").fill(itemBody);
  await page.getByRole("button", { name: "提交到收件箱" }).click();
  await expect(page.getByText("任务状态：succeeded")).toBeVisible();
  await expect(page.getByRole("button", { name: "审核通过" })).toBeVisible();

  await page.getByRole("button", { name: "审核通过" }).click();
  await expect(page.getByRole("button", { name: "发布到 Obsidian" })).toBeVisible();
  await page.getByRole("button", { name: "发布到 Obsidian" }).click();
  await expect(page.getByText("当前没有待处理草稿。")).toBeVisible();

  await page.getByRole("button", { name: "搜索与问答" }).click();
  await expect(page.getByRole("heading", { name: "搜索与问答" })).toBeVisible();
  await page.getByLabel("搜索问题或关键词").fill("SQLite");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.getByRole("heading", { name: "当前知识证据" })).toBeVisible();
  await expect(page.locator(".citation-card")).toContainText("合成 E2E 知识");
  await expect(page.locator(".citation-card")).toContainText("精确定位");
  await expect(page.getByRole("button", { name: "在 Obsidian 打开 ↗" })).toBeVisible();

  await page.getByRole("button", { name: "基于证据回答" }).click();
  await expect(page.getByRole("heading", { name: "回答" })).toBeVisible();
  await expect(page.locator(".answer-copy p")).toContainText("SQLite 是当前回答使用的权威校验来源。");
  await expect(page.locator(".citation-chip")).toHaveText("C1");
  expect(observed).toEqual(
    expect.arrayContaining([
      "POST /api/sources/text",
      `POST /api/items/${itemId}/review`,
      `POST /api/items/${itemId}/publish`,
      "POST /api/search",
      "POST /api/chat/stream",
    ]),
  );
});

test("synthetic collections page lists a collection and removes a member", async ({ page }) => {
  let hasMember = true;
  const observed: string[] = [];

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    observed.push(`${request.method()} ${path}`);

    if (request.method() === "GET" && path === "/api/dashboard") {
      await route.fulfill({ json: dashboard("published") });
      return;
    }
    if (request.method() === "GET" && path === "/api/collections") {
      await route.fulfill({ json: [collectionSummary()] });
      return;
    }
    if (request.method() === "GET" && path === "/api/items") {
      expect(url.searchParams.get("status")).toBe("published");
      await route.fulfill({ json: [item("published")] });
      return;
    }
    if (request.method() === "GET" && path === `/api/collections/${collectionId}`) {
      await route.fulfill({ json: collectionDetail(hasMember) });
      return;
    }
    if (
      request.method() === "DELETE" &&
      path === `/api/collections/${collectionId}/items/${itemId}`
    ) {
      hasMember = false;
      await route.fulfill({ json: collectionDetail(false) });
      return;
    }
    throw new Error(`Unhandled synthetic API request: ${request.method()} ${path}`);
  });

  await page.goto("/");
  await page.getByRole("button", { name: "合集" }).click();
  await expect(page.getByRole("heading", { name: "合集", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "合成阅读合集", exact: true })).toBeVisible();
  await expect(page.getByText("合成 E2E 知识", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "移除", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("知识条目已移出合集");
  await expect(
    page.getByText("还没有成员；只能加入已发布的知识条目。", { exact: true }),
  ).toBeVisible();
  expect(observed).toEqual(
    expect.arrayContaining([
      "GET /api/collections",
      "GET /api/items",
      `GET /api/collections/${collectionId}`,
      `DELETE /api/collections/${collectionId}/items/${itemId}`,
    ]),
  );
});

test("synthetic settings page shows capabilities and completes a backup", async ({ page }) => {
  const observed: string[] = [];

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    observed.push(`${request.method()} ${path}`);

    if (request.method() === "GET" && path === "/api/dashboard") {
      await route.fulfill({ json: dashboard("published") });
      return;
    }
    if (request.method() === "GET" && path === "/api/settings") {
      await route.fulfill({ json: settingsResponse() });
      return;
    }
    if (request.method() === "POST" && path === "/api/settings/backup") {
      await route.fulfill({
        status: 201,
        json: {
          archive_id: "backup-" + "a".repeat(32),
          created_at: "2026-08-29T00:00:00Z",
          sha256: "b".repeat(64),
          config_key: "BACKUP_ROOT",
        },
      });
      return;
    }
    throw new Error(`Unhandled synthetic API request: ${request.method()} ${path}`);
  });

  page.on("dialog", async (dialog) => {
    await dialog.accept();
  });
  await page.goto("/");
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page.getByRole("heading", { name: "设置", exact: true })).toBeVisible();
  for (const provider of ["Chat", "Embedding", "ASR", "Vision", "Reranker"]) {
    await expect(page.getByRole("heading", { name: provider, exact: true })).toBeVisible();
  }
  await expect(
    page.getByText("FFmpeg 未配置只影响视频能力；请稍后按人工步骤安装。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "重新扫描", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建备份", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "重建派生索引", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "创建备份", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("备份已创建");
  expect(observed).toEqual(
    expect.arrayContaining(["GET /api/settings", "POST /api/settings/backup"]),
  );
});

test("synthetic capture modes, review editor, and review decisions", async ({ page }) => {
  const approveId = "77777777-7777-4777-8777-777777777777";
  const rejectId = "88888888-8888-4888-8888-888888888888";
  const cancelId = "99999999-9999-4999-8999-999999999999";
  type ReviewState = "pending_review" | "reviewed" | "published" | "failed" | "cancelled";
  const reviewStates = new Map<string, ReviewState>([
    [approveId, "pending_review"],
    [rejectId, "pending_review"],
    [cancelId, "pending_review"],
  ]);
  const reviewFields = new Map<string, { title: string; body: string; summary: string; tags: string[]; collections: string[] }>();
  const observed: string[] = [];
  let jobNumber = 0;
  let fileSubmissionNumber = 0;
  const captureJobIds = new Set<string>();

  const reviewProjection = (id: string) => {
    const state = reviewStates.get(id) ?? "pending_review";
    const fields = reviewFields.get(id) ?? {
      title: id === approveId ? "可编辑审核" : id === rejectId ? "拒绝候选" : "取消候选",
      body: "待审核正文",
      summary: "待审核摘要",
      tags: ["测试"],
      collections: ["合成阅读合集"],
    };
    const active = state === "pending_review" || state === "reviewed";
    const base = item(state === "published" ? "published" : state === "reviewed" ? "reviewed" : "pending_review");
    return {
      ...base,
      id,
      title: fields.title,
      body: fields.body,
      summary: fields.summary,
      suggested_tags: fields.tags,
      suggested_collections: fields.collections,
      confirmed_tags: state === "published" ? fields.tags : [],
      collections: state === "published" ? fields.collections : [],
      status: state,
      current_content_version_id: state === "published" ? versionId : null,
      pending_content_version_id: active ? versionId : null,
      has_pending_review: active,
    };
  };

  const queuedReviews = () =>
    [...reviewStates.keys()]
      .map(reviewProjection)
      .filter((candidate) => candidate.status === "pending_review" || candidate.status === "reviewed");

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    observed.push(`${request.method()} ${path}`);

    if (request.method() === "GET" && path === "/api/dashboard") {
      await route.fulfill({ json: dashboard("pending_review") });
      return;
    }
    if (request.method() === "GET" && path === "/api/items") {
      await route.fulfill({ json: queuedReviews() });
      return;
    }
    const itemMatch = path.match(/^\/api\/items\/([^/]+)$/);
    if (request.method() === "GET" && itemMatch) {
      const id = decodeURIComponent(itemMatch[1]);
      if (!reviewStates.has(id)) {
        throw new Error(`Unknown synthetic review item: ${id}`);
      }
      await route.fulfill({ json: reviewProjection(id) });
      return;
    }
    if (request.method() === "POST" && path === "/api/sources/text") {
      const body = request.postDataJSON() as { content?: string; source_type?: string };
      expect(body.content).toBeTruthy();
      expect(body.source_type).toBe(fileSubmissionNumber === 0 ? "markdown" : "text");
      fileSubmissionNumber += 1;
      const captureJobId = "capture-job-" + String(++jobNumber);
      captureJobIds.add(captureJobId);
      await route.fulfill({
        status: 202,
        json: { item_id: approveId, job_id: captureJobId, deduplicated: false },
      });
      return;
    }
    if (request.method() === "POST" && path === "/api/sources/files") {
      const contentType = request.headers()["content-type"] ?? "";
      expect(contentType).toMatch(/^multipart\/form-data; boundary=/);
      fileSubmissionNumber += 1;
      const captureJobId = "capture-job-" + String(++jobNumber);
      captureJobIds.add(captureJobId);
      await route.fulfill({
        status: 202,
        json: {
          item_id: approveId,
          job_id: captureJobId,
          deduplicated: fileSubmissionNumber === 4,
        },
      });
      return;
    }
    if (request.method() === "POST" && path === "/api/sources/url") {
      const body = request.postDataJSON() as { url?: string; title?: string };
      expect(body.url).toBe("https://example.test/article");
      expect(body.title).toBe("合成网页");
      const captureJobId = "capture-job-" + String(++jobNumber);
      captureJobIds.add(captureJobId);
      await route.fulfill({
        status: 202,
        json: { item_id: approveId, job_id: captureJobId, deduplicated: false },
      });
      return;
    }
    if (request.method() === "POST" && path === "/api/sources/video") {
      const body = request.postDataJSON() as {
        url?: string;
        title?: string;
        language?: string;
        enable_vision?: boolean;
      };
      expect(body.url).toBe("https://video.example.test/watch");
      expect(body.title).toBe("合成视频");
      expect(body.language).toBe("zh-Hans");
      expect(body.enable_vision).toBe(true);
      const captureJobId = "capture-job-" + String(++jobNumber);
      captureJobIds.add(captureJobId);
      await route.fulfill({
        status: 202,
        json: { item_id: approveId, job_id: captureJobId, deduplicated: false },
      });
      return;
    }
    const jobMatch = path.match(/^\/api\/jobs\/([^/]+)$/);
    if (request.method() === "GET" && jobMatch) {
      const id = decodeURIComponent(jobMatch[1]);
      if (!captureJobIds.has(id)) {
        throw new Error(`Unknown synthetic job: ${id}`);
      }
      await route.fulfill({
        json: {
          id,
          kind: "ingest_source",
          state: "succeeded",
          stage: "completed",
          progress: 1,
          retry_count: 0,
          max_retries: 3,
          error: null,
          result: { item_id: approveId },
          heartbeat_at: now,
          created_at: now,
        },
      });
      return;
    }
    const reviewMatch = path.match(/^\/api\/items\/([^/]+)\/review$/);
    if (request.method() === "POST" && reviewMatch) {
      const id = decodeURIComponent(reviewMatch[1]);
      const body = request.postDataJSON() as {
        decision?: string;
        title?: string;
        body?: string;
        summary?: string;
        suggested_tags?: string[];
        suggested_collections?: string[];
      };
      expect(reviewStates.has(id)).toBe(true);
      if (id === approveId) {
        expect(body.decision).toBe("approve");
        expect(body.title).toBe("确认后的标题");
        expect(body.body).toContain("确认后的 Markdown 正文");
        expect(body.summary).toBe("确认后的摘要");
        expect(body.suggested_tags).toEqual(["已确认标签", "第二标签"]);
        expect(body.suggested_collections).toEqual(["已确认合集"]);
        reviewFields.set(id, {
          title: body.title ?? "",
          body: body.body ?? "",
          summary: body.summary ?? "",
          tags: body.suggested_tags ?? [],
          collections: body.suggested_collections ?? [],
        });
        reviewStates.set(id, "reviewed");
      } else if (id === rejectId) {
        expect(body.decision).toBe("reject");
        reviewStates.set(id, "failed");
      } else {
        expect(body.decision).toBe("cancel");
        reviewStates.set(id, "cancelled");
      }
      await route.fulfill({ json: reviewProjection(id) });
      return;
    }
    const publishMatch = path.match(/^\/api\/items\/([^/]+)\/publish$/);
    if (request.method() === "POST" && publishMatch) {
      const id = decodeURIComponent(publishMatch[1]);
      expect(id).toBe(approveId);
      expect(reviewStates.get(id)).toBe("reviewed");
      reviewStates.set(id, "published");
      await route.fulfill({ json: reviewProjection(id) });
      return;
    }
    throw new Error(`Unhandled synthetic API request: ${request.method()} ${path}`);
  });

  await page.goto("/");
  await page.getByRole("button", { name: "上传文件" }).click();
  await expect(page.getByRole("button", { name: "文件" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "今日总览" }).click();
  await page.getByRole("button", { name: "添加网页" }).click();
  await expect(page.getByRole("button", { name: "网页" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "今日总览" }).click();
  await page.getByRole("button", { name: "添加视频" }).click();
  await expect(page.getByRole("button", { name: "视频" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "今日总览" }).click();
  await page.getByRole("button", { name: "上传文件" }).click();

  await page.locator("#source-file-input").setInputFiles([
    { name: "合成笔记.md", mimeType: "text/markdown", buffer: Buffer.from("# Markdown\nSQLite") },
    { name: "合成文本.txt", mimeType: "text/plain", buffer: Buffer.from("TXT SQLite") },
    { name: "合成文档.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF synthetic") },
    {
      name: "合成文档.docx",
      mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      buffer: Buffer.from("PK synthetic docx"),
    },
  ]);
  await expect(page.getByRole("list", { name: "待提交文件" })).toContainText("合成笔记.md");
  await expect(page.getByRole("list", { name: "待提交文件" })).toContainText("合成文档.docx");
  await page.getByRole("button", { name: "提交文件批次" }).click();
  await expect(page.getByRole("status")).toContainText("批次完成：4/4 项已提交或去重");

  await page.getByRole("button", { name: "网页" }).click();
  await page.getByLabel("网页 URL").fill("https://example.test/article");
  await page.getByLabel("网页标题").fill("合成网页");
  await page.getByRole("button", { name: "提交网页" }).click();
  await expect(page.getByRole("status")).toContainText("已提交，草稿已进入人工确认队列");

  await page.getByRole("button", { name: "视频" }).click();
  await page.getByLabel("视频 URL").fill("https://video.example.test/watch");
  await page.getByLabel("视频标题").fill("合成视频");
  await page.getByLabel("字幕语言").fill("zh-Hans");
  await page.getByLabel("启用条件视觉处理").check();
  await page.getByRole("button", { name: "提交视频" }).click();
  await expect(page.getByRole("status")).toContainText("已提交，草稿已进入人工确认队列");

  await expect(page.getByRole("button", { name: "查看审核详情" })).toHaveCount(3);
  const approveCard = page.locator("article.review-item").filter({ hasText: "可编辑审核" });
  await approveCard.getByRole("button", { name: "查看审核详情" }).click();
  const detail = page.locator('aside[aria-label="审核详情"]');
  await expect(detail).toContainText("来源：markdown");
  await expect(detail).toContainText("待审核摘要");
  await page.getByLabel("审核标题").fill("确认后的标题");
  await page.getByLabel("审核正文").fill("确认后的 Markdown 正文");
  await page.getByLabel("AI 摘要").fill("确认后的摘要");
  await page.getByLabel("建议标签").fill("已确认标签、第二标签");
  await page.getByLabel("建议合集").fill("已确认合集");
  await page.getByRole("button", { name: "确认审核" }).click();
  await expect(page.getByRole("status")).toContainText("审核已确认");
  await detail.getByRole("button", { name: "发布到 Obsidian" }).click();
  await expect(page.getByRole("status")).toContainText("已发布到 Obsidian");

  const rejectCard = page.locator("article.review-item").filter({ hasText: "拒绝候选" });
  await rejectCard.getByRole("button", { name: "查看审核详情" }).click();
  await page.locator('aside[aria-label="审核详情"]').getByRole("button", { name: "拒绝", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已拒绝当前候选版本");

  const cancelCard = page.locator("article.review-item").filter({ hasText: "取消候选" });
  await cancelCard.getByRole("button", { name: "查看审核详情" }).click();
  await page.locator('aside[aria-label="审核详情"]').getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已取消当前审核操作");
  expect(observed).toEqual(
    expect.arrayContaining([
      "POST /api/sources/text",
      "POST /api/sources/files",
      "POST /api/sources/url",
      "POST /api/sources/video",
      `POST /api/items/${approveId}/review`,
      `POST /api/items/${approveId}/publish`,
      `POST /api/items/${rejectId}/review`,
      `POST /api/items/${cancelId}/review`,
    ]),
  );
});

test("synthetic knowledge library protects edits, reprocesses, and soft deletes", async ({ page }) => {
  const libraryId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const libraryVersionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const pendingVersionId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const observed: string[] = [];
  let currentHash = "c".repeat(64);
  let saved = false;
  let patchCount = 0;
  let reprocessed = false;
  let deleted = false;

  const libraryItem = () => ({
    id: libraryId,
    title: saved ? "已保存标题" : "合成知识详情",
    source_type: "markdown",
    status: "published",
    content_hash: currentHash,
    current_content_version_id: libraryVersionId,
    pending_content_version_id: reprocessed ? pendingVersionId : null,
    has_pending_review: reprocessed,
    body: saved ? "已保存的 Markdown 正文" : "当前 Markdown 正文",
    summary: "可公开的合成摘要",
    suggested_tags: ["工程"],
    suggested_collections: ["阅读"],
    confirmed_tags: ["工程"],
    collections: ["阅读"],
    source_metadata: {
      source_url: "https://example.test/article",
      author: "合成来源",
      segments: ["内部分段不应显示"],
    },
    version_no: 2,
    note_relative_path: "知流台/合成知识.md",
    sync_state: "synced",
    created_at: now,
    updated_at: now,
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    observed.push(`${request.method()} ${path}`);

    if (request.method() === "GET" && path === "/api/dashboard") {
      await route.fulfill({ json: dashboard("published") });
      return;
    }
    if (request.method() === "GET" && path === "/api/items") {
      expect(url.searchParams.get("status")).toBe("published");
      if (url.searchParams.get("tag")) {
        expect(url.searchParams.get("source_type")).toBe("markdown");
        expect(url.searchParams.get("tag")).toBe("工程");
        expect(url.searchParams.get("collection")).toBe("阅读");
        expect(url.searchParams.get("created_after")).toBe("2026-08-01");
        expect(url.searchParams.get("created_before")).toBe("2026-08-30");
      }
      await route.fulfill({ json: deleted ? [] : [libraryItem()] });
      return;
    }
    if (request.method() === "GET" && path === "/api/obsidian/status") {
      await route.fulfill({
        json: {
          configured: true,
          watcher_running: true,
          managed_directory: "C:\\Users\\Synthetic\\Vault",
          last_heartbeat_at: now,
          last_error: null,
        },
      });
      return;
    }
    if (request.method() === "GET" && path === `/api/items/${libraryId}`) {
      await route.fulfill({ json: libraryItem() });
      return;
    }
    if (request.method() === "PATCH" && path === `/api/items/${libraryId}`) {
      const body = request.postDataJSON() as { title?: string; body?: string; expected_content_hash?: string };
      expect(body.expected_content_hash).toBe(currentHash);
      patchCount += 1;
      if (patchCount === 1) {
        expect(body.title).toBe("已保存标题");
        expect(body.body).toBe("已保存的 Markdown 正文");
        saved = true;
        currentHash = "d".repeat(64);
        await route.fulfill({ json: libraryItem() });
      } else {
        await route.fulfill({
          status: 409,
          json: { error: { code: "content_conflict", message: "内容哈希不匹配" } },
        });
      }
      return;
    }
    if (request.method() === "POST" && path === `/api/items/${libraryId}/reprocess`) {
      reprocessed = true;
      await route.fulfill({
        status: 202,
        json: { item_id: libraryId, job_id: "reprocess-job-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", deduplicated: false },
      });
      return;
    }
    if (request.method() === "DELETE" && path === `/api/items/${libraryId}`) {
      deleted = true;
      await route.fulfill({ status: 204 });
      return;
    }
    throw new Error(`Unhandled synthetic API request: ${request.method()} ${path}`);
  });

  page.on("dialog", async (dialog) => {
    expect(dialog.message()).toContain("Markdown 正文不会被删除");
    await dialog.accept();
  });
  await page.goto("/");
  await page.getByRole("button", { name: "知识库" }).click();
  await expect(page.getByRole("heading", { name: "知识库", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "合成知识详情", exact: true })).toBeVisible();
  await expect(page.getByText("受管理目录已隐藏", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("C:\\Users\\Synthetic\\Vault");
  await expect(page.getByText("当前 Markdown 正文", { exact: true })).toBeVisible();

  await page.getByLabel("知识来源").selectOption("markdown");
  await page.getByLabel("筛选标签").fill("工程");
  await page.getByLabel("筛选合集").fill("阅读");
  await page.getByLabel("起始日期").fill("2026-08-01");
  await page.getByLabel("结束日期").fill("2026-08-30");
  await page.getByRole("button", { name: "应用筛选" }).click();
  await expect(page.getByRole("heading", { name: "合成知识详情", exact: true })).toBeVisible();

  await page.getByLabel("标题").fill("已保存标题");
  await page.getByLabel("Markdown 正文").fill("已保存的 Markdown 正文");
  await page.getByRole("button", { name: "保存 Markdown" }).click();
  await expect(page.getByRole("status")).toContainText("Markdown 已保存");

  await page.getByLabel("Markdown 正文").fill("第二次编辑，模拟外部修改冲突");
  await page.getByRole("button", { name: "保存 Markdown" }).click();
  await expect(page.getByRole("alert")).toContainText("Obsidian 内容已变化");

  await page.getByRole("button", { name: "重处理" }).click();
  await expect(page.getByRole("status")).toContainText("已提交重处理");
  await page.getByRole("button", { name: "重新载入" }).click();
  await expect(page.getByRole("note")).toContainText("待审核版本");
  await page.getByRole("button", { name: "软删除" }).click();
  await expect(page.getByRole("status")).toContainText("Markdown 正文仍保留在 Obsidian");
  expect(observed).toEqual(
    expect.arrayContaining([
      "GET /api/items",
      `GET /api/items/${libraryId}`,
      `PATCH /api/items/${libraryId}`,
      `POST /api/items/${libraryId}/reprocess`,
      `DELETE /api/items/${libraryId}`,
    ]),
  );
});

test("synthetic dashboard jobs recovery and evidence refusal", async ({ page }) => {
  const failedJobId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const runningJobId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const observed: string[] = [];
  let failedState: "failed" | "queued" = "failed";
  let runningState: "running" | "cancelled" = "running";
  let chatCalls = 0;

  const jobList = () => [
    {
      id: failedJobId,
      kind: "ingest_url",
      state: failedState,
      stage: failedState === "failed" ? "fetch" : "queued",
      progress: failedState === "failed" ? 0.4 : 0,
      retry_count: failedState === "failed" ? 1 : 2,
      max_retries: 3,
      error: failedState === "failed" ? { code: "source_timeout", message: "上游服务暂时不可用" } : null,
      result: null,
      heartbeat_at: now,
      created_at: now,
      started_at: now,
      finished_at: failedState === "failed" ? now : null,
      duration_ms: failedState === "failed" ? 3200 : null,
      attempts: [
        {
          id: "attempt-dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          attempt_no: 1,
          state: "failed",
          stage: "fetch",
          started_at: now,
          heartbeat_at: now,
          finished_at: now,
          duration_ms: 3200,
          error: { code: "source_timeout", message: "上游服务暂时不可用" },
        },
      ],
    },
    {
      id: runningJobId,
      kind: "ingest_video",
      state: runningState,
      stage: "transcript",
      progress: runningState === "running" ? 0.65 : 1,
      retry_count: 0,
      max_retries: 3,
      error: null,
      result: null,
      heartbeat_at: now,
      created_at: now,
      started_at: now,
      finished_at: runningState === "cancelled" ? now : null,
      duration_ms: runningState === "cancelled" ? 1800 : null,
      attempts: [],
    },
  ];

  const dashboardWithActivity = () => ({
    ...dashboard("pending_review"),
    stats: { knowledge_count: 2, today_added: 1, pending_review: 1, processing: 2 },
    pending_reviews: [
      { id: itemId, title: "合成待审核", source_type: "markdown", status: "pending_review", updated_at: now },
    ],
    recent_items: [
      { id: itemId, title: "合成最近知识", source_type: "markdown", status: "published", updated_at: now },
    ],
    processing_jobs: [
      {
        id: runningJobId,
        kind: "ingest_video",
        state: runningState,
        stage: "transcript",
        progress: 0.65,
        heartbeat_at: now,
        started_at: now,
        finished_at: runningState === "cancelled" ? now : null,
        duration_ms: runningState === "cancelled" ? 1800 : null,
        error: null,
      },
    ],
  });

  const refusalStream = () =>
    [
      "event: meta",
      `data: ${JSON.stringify({
        query: "无证据",
        normalized_query: "无证据",
        evidence: { status: "none", reason: "没有足够的当前版本证据" },
        diagnostics: {
          original_query: "无证据",
          normalized_query: "无证据",
          fts_query: "无证据",
          fts_available: true,
          vector_available: true,
          degraded: false,
          channel_errors: {},
          reranker_available: false,
        },
        rewrite_status: "off",
        refusal: "没有足够的当前版本证据，无法回答。",
      })}`,
      "",
      "event: delta",
      `data: ${JSON.stringify({ text: "没有足够的当前版本证据，无法回答。", citation_ids: [] })}`,
      "",
      "event: citations",
      `data: ${JSON.stringify({ citations: [] })}`,
      "",
      "event: done",
      `data: ${JSON.stringify({ answer: null, conflicts: [], model_run_id: null })}`,
      "",
    ].join("\n");

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    observed.push(`${request.method()} ${path}`);

    if (request.method() === "GET" && path === "/api/dashboard") {
      await route.fulfill({ json: dashboardWithActivity() });
      return;
    }
    if (request.method() === "GET" && path === "/api/items") {
      await route.fulfill({ json: [] });
      return;
    }
    if (request.method() === "GET" && path === "/api/jobs") {
      await route.fulfill({ json: jobList() });
      return;
    }
    if (request.method() === "POST" && path === `/api/jobs/${failedJobId}/retry`) {
      failedState = "queued";
      await route.fulfill({ json: jobList()[0] });
      return;
    }
    if (request.method() === "POST" && path === `/api/jobs/${runningJobId}/cancel`) {
      runningState = "cancelled";
      await route.fulfill({ json: jobList()[1] });
      return;
    }
    if (request.method() === "POST" && path === "/api/search") {
      const body = request.postDataJSON() as { query?: string };
      expect(body.query).toBe("无证据");
      await route.fulfill({
        json: {
          query: "无证据",
          normalized_query: "无证据",
          results: [],
          evidence: { status: "none", reason: "没有足够的当前版本证据" },
          diagnostics: {
            original_query: "无证据",
            normalized_query: "无证据",
            fts_query: "无证据",
            fts_available: true,
            vector_available: true,
            degraded: false,
            channel_errors: {},
            reranker_available: false,
          },
          searched_at: now,
        },
      });
      return;
    }
    if (request.method() === "POST" && path === "/api/chat/stream") {
      chatCalls += 1;
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream; charset=utf-8" },
        body: refusalStream(),
      });
      return;
    }
    throw new Error(`Unhandled synthetic API request: ${request.method()} ${path}`);
  });

  await page.goto("/");
  await expect(page.getByText("合成待审核", { exact: true })).toBeVisible();
  await expect(page.getByText("合成最近知识", { exact: true })).toBeVisible();
  await expect(page.getByText("ingest_video", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "收件箱", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "今日总览" }).click();
  await page.getByRole("button", { name: "查看任务" }).click();
  await expect(page.getByRole("heading", { name: "处理任务", exact: true })).toBeVisible();
  await expect(page.getByText("上游服务暂时不可用", { exact: true })).toBeVisible();
  await expect(page.getByText("最后 heartbeat", { exact: true })).toBeVisible();
  await expect(page.getByText("JobAttempt 历史（1）", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("任务已重新排队");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("任务已取消");

  await page.getByRole("button", { name: "搜索与问答" }).click();
  await page.getByLabel("搜索问题或关键词").fill("无证据");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.getByRole("heading", { name: "当前知识证据" })).toBeVisible();
  await expect(page.getByText("没有找到当前版本证据", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "基于证据回答" }).click();
  await expect(page.getByRole("heading", { name: "回答" })).toBeVisible();
  await expect(page.locator(".answer-copy")).toContainText("没有足够的当前版本证据");
  expect(chatCalls).toBe(1);
  expect(observed).toEqual(
    expect.arrayContaining([
      "GET /api/dashboard",
      "GET /api/jobs",
      `POST /api/jobs/${failedJobId}/retry`,
      `POST /api/jobs/${runningJobId}/cancel`,
      "POST /api/search",
      "POST /api/chat/stream",
    ]),
  );
});
