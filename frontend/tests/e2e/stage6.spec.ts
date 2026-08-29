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
      ? [{ id: itemId, title: "合成 E2E 知识", source_type: "markdown" }]
      : [],
    recent_items: status === "published" ? [{ id: itemId, title: "合成 E2E 知识", status }] : [],
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
