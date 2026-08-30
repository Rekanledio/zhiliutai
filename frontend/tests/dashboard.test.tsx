import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import App from "../src/app/App";

const dashboard = {
  greeting: "测试问候",
  date_label: "8 月 30 日 · 星期日",
  stats: { knowledge_count: 1, today_added: 2, pending_review: 1, processing: 1 },
  health: { status: "healthy", checked_at: "2026-08-30T00:00:00Z", components: [] },
  pending_reviews: [
    {
      id: "pending-1",
      title: "待确认条目",
      source_type: "markdown",
      status: "pending_review",
      updated_at: "2026-08-30T10:00:00Z",
    },
  ],
  recent_items: [
    {
      id: "published-1",
      title: "最近已发布",
      source_type: "text",
      status: "published",
      updated_at: "2026-08-30T09:00:00Z",
    },
  ],
  processing_jobs: [
    {
      id: "job-1",
      kind: "ingest_text",
      state: "running",
      stage: "embedding",
      progress: 0.45,
      started_at: "2026-08-30T08:00:00Z",
      heartbeat_at: "2026-08-30T08:01:00Z",
      finished_at: null,
      duration_ms: 60000,
      error: null,
    },
  ],
};

function responseWith(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": "dashboard-test" }),
    json: async () => body,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("首页渲染真实 pending/recent/jobs 并导航到实际页面", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/dashboard") return responseWith(dashboard);
    if (url.startsWith("/api/items")) return responseWith([]);
    if (url === "/api/obsidian/status") return responseWith({ configured: false, watcher_running: false });
    if (url === "/api/jobs") return responseWith([]);
    throw new Error("unhandled route " + url);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  expect(await screen.findByText("待确认条目")).toBeTruthy();
  expect(screen.getByText("最近已发布")).toBeTruthy();
  expect(
    screen.getByText((_, element) =>
      Boolean(element?.tagName === "SMALL" && element.textContent?.includes("45%")),
    ),
  ).toBeTruthy();
  expect(screen.getByText("本地今天已收录")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "确认" }));
  expect(await screen.findByRole("heading", { level: 1, name: "收件箱" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /^今日总览/ }));
  fireEvent.click(screen.getByRole("button", { name: /进入知识库/ }));
  expect(await screen.findByRole("heading", { level: 1, name: "知识库" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /^今日总览/ }));
  fireEvent.click(screen.getByRole("button", { name: /查看任务/ }));
  expect(await screen.findByRole("heading", { level: 1, name: "处理任务" })).toBeTruthy();
});
