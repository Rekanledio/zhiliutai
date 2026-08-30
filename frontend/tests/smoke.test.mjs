import { createElement } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import App from "../src/app/App";

const dashboardResponse = {
  greeting: "测试问候",
  date_label: "8 月 26 日 · 星期三",
  stats: {
    knowledge_count: 12,
    today_added: 2,
    pending_review: 1,
    processing: 0,
  },
  health: {
    status: "degraded",
    checked_at: "2026-08-26T00:00:00+08:00",
    components: [
      { key: "api", label: "FastAPI", state: "healthy", detail: "服务进程正常" },
      { key: "sqlite", label: "SQLite", state: "healthy", detail: "本地数据库可用" },
      { key: "qdrant", label: "Qdrant Local", state: "healthy", detail: "本地客户端可用" },
      { key: "artifact_storage", label: "Artifact Storage", state: "healthy", detail: "目录可读写" },
      { key: "obsidian", label: "Obsidian Vault", state: "healthy", detail: "目录可读写" },
      { key: "obsidian_watcher", label: "Obsidian Watcher", state: "healthy", detail: "监听中" },
      {
        key: "model_providers",
        label: "Model Providers",
        state: "configured",
        detail: "本地能力：Embedding=FastEmbed；远程能力：Chat=未配置",
      },
      { key: "ffmpeg", label: "FFmpeg", state: "not_configured", detail: "仅影响视频" },
    ],
  },
  pending_reviews: [],
  recent_items: [],
  processing_jobs: [],
};

const settingsResponse = {
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
      configured: false,
      credential_configured: false,
      model: null,
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
  chunking: { strategy: "paragraph_then_fixed_width", max_chars: 800 },
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

const baseItem = {
  id: "item-1",
  title: "阶段 2 草稿",
  source_type: "markdown",
  status: "pending_review",
  content_hash: "a".repeat(64),
  body: "# 阶段 2 草稿",
  summary: "测试摘要",
  suggested_tags: ["测试"],
  version_no: 1,
  note_relative_path: null,
  sync_state: null,
  created_at: "2026-08-26T00:00:00+08:00",
  updated_at: "2026-08-26T00:00:00+08:00",
};

function responseWith(body, status = 200, requestId = "response-request") {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": requestId }),
    json: async () => body,
  };
}

function appFetch(overrides = {}) {
  let currentItem = { ...baseItem };
  let items = overrides.items ?? [];
  return vi.fn(async (url, init = {}) => {
    const method = init.method ?? "GET";
    if (url === "/api/dashboard") return responseWith(dashboardResponse);
    if (url === "/api/settings") return responseWith(settingsResponse);
    if (url === "/api/items" || String(url).startsWith("/api/items?")) {
      return responseWith(items);
    }
    if (url === "/api/jobs") return responseWith([]);
    if (url === "/api/obsidian/status") {
      return responseWith({ configured: true, watcher_running: true, managed_directory: "知流台" });
    }
    if (url === "/api/sources/text" && method === "POST") {
      if (overrides.submitError) {
        return responseWith(
          { error: { code: "internal_error", message: "服务内部错误", request_id: "server-500" } },
          500,
          "server-500",
        );
      }
      items = [currentItem];
      return responseWith({ item_id: "item-1", job_id: "job-1", deduplicated: false }, 202);
    }
    if (url === "/api/jobs/job-1") {
      return responseWith({
        id: "job-1",
        kind: "ingest_text",
        state: "succeeded",
        stage: "complete",
        progress: 1,
        retry_count: 0,
        max_retries: 3,
        created_at: "2026-08-26T00:00:00+08:00",
      });
    }
    if (url === "/api/items/item-1/review" && method === "POST") {
      currentItem = { ...currentItem, status: "reviewed" };
      items = [currentItem];
      return responseWith(currentItem);
    }
    if (url === "/api/items/item-1/publish" && method === "POST") {
      currentItem = {
        ...currentItem,
        status: "published",
        note_relative_path: "Notes/test.md",
        sync_state: "synced",
      };
      items = [currentItem];
      return responseWith(currentItem);
    }
    if (url === "/api/items/item-1") return responseWith(currentItem);
    throw new Error("unhandled mock route: " + method + " " + url);
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("阶段 1/2 React 工作台", () => {
  test("请求并渲染最终架构的真实健康状态", async () => {
    const fetchMock = appFetch();
    vi.stubGlobal("fetch", fetchMock);
    render(createElement(App));

    expect(
      await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" }),
    ).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/dashboard",
      expect.objectContaining({
        headers: expect.objectContaining({ Accept: "application/json" }),
        signal: expect.any(AbortSignal),
      }),
    );
    expect(screen.getAllByText("SQLite").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Qdrant Local").length).toBeGreaterThan(0);
    expect(screen.getAllByText("未配置").length).toBeGreaterThan(0);
    expect(screen.queryByText("已配置（未验证）")).toBeNull();
    const modelStatusBadges = screen.getAllByLabelText(
      "已配置：本地能力：Embedding=FastEmbed；远程能力：Chat=未配置",
    );
    expect(modelStatusBadges).toHaveLength(2);
    for (const badge of modelStatusBadges) {
      expect(badge.getAttribute("title")).toBe(
        "本地能力：Embedding=FastEmbed；远程能力：Chat=未配置",
      );
      expect(badge.className).toContain("is-configured");
    }
  });

  test("展示七项导航并切换阶段 2 与设置页面", async () => {
    vi.stubGlobal("fetch", appFetch());
    render(createElement(App));
    await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" });
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    const labels = ["今日总览", "收件箱", "知识库", "合集", "搜索与问答", "处理任务", "设置"];
    for (const label of labels) {
      expect(within(navigation).getByRole("button", { name: new RegExp("^" + label) })).toBeTruthy();
    }
    for (const label of labels.slice(1)) {
      fireEvent.click(within(navigation).getByRole("button", { name: new RegExp("^" + label) }));
      expect(await screen.findByRole("heading", { level: 1, name: label })).toBeTruthy();
    }
    expect(screen.getByText("这里只观察安全配置并执行受控维护；API Key 不会返回到前端。")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "模型能力" })).toBeTruthy();
    expect(screen.queryByText("页面入口已经保留，相关功能正在本轮补齐。")).toBeNull();
    expect(screen.queryByText(/阶段 4/)).toBeNull();
  });

  test("首页粘贴文本入口进入真实收件箱", async () => {
    vi.stubGlobal("fetch", appFetch());
    render(createElement(App));
    await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" });
    fireEvent.click(screen.getByRole("button", { name: /粘贴文本/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "收件箱" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "知识内容" })).toBeTruthy();
  });

  test("提交文本、显示任务结果、审核并发布", async () => {
    vi.stubGlobal("fetch", appFetch());
    render(createElement(App));
    await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" });
    fireEvent.click(screen.getByRole("button", { name: /收件箱/ }));
    const input = await screen.findByRole("textbox", { name: "知识内容" });
    fireEvent.change(input, { target: { value: "# 阶段 2 草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
    fireEvent.click(screen.getByRole("button", { name: "提交到收件箱" }));
    expect(await screen.findByText("阶段 2 草稿")).toBeTruthy();
    expect(screen.getByText("任务状态：已完成")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "审核通过" }));
    expect(await screen.findByRole("button", { name: "发布到 Obsidian" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发布到 Obsidian" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "发布到 Obsidian" })).toBeNull(),
    );
  });

  test("知识库展示 Obsidian watcher 状态", async () => {
    vi.stubGlobal("fetch", appFetch({ items: [{ ...baseItem, status: "published" }] }));
    render(createElement(App));
    await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" });
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    fireEvent.click(within(navigation).getByRole("button", { name: /^知识库/ }));
    expect(await screen.findByText("监听中 · 知流台")).toBeTruthy();
  });

  test("API 错误保留后端 Request ID", async () => {
    vi.stubGlobal("fetch", appFetch({ submitError: true }));
    render(createElement(App));
    await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" });
    fireEvent.click(screen.getByRole("button", { name: /收件箱/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "知识内容" }), {
      target: { value: "会失败的输入" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交到收件箱" }));
    expect((await screen.findByRole("alert")).textContent).toContain("server-500");
  });

  test("后端不可用时显示离线降级骨架", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("API offline")));
    render(createElement(App));
    expect(await screen.findByText("后端暂不可用，当前显示本机离线骨架")).toBeTruthy();
    expect(screen.getAllByText("降级").length).toBeGreaterThan(0);
  });
});
