import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { KnowledgePage } from "../src/features/knowledge/KnowledgePage";

const baseItem = {
  id: "item-1",
  title: "可编辑知识",
  source_type: "markdown",
  status: "published",
  content_hash: "a".repeat(64),
  current_content_version_id: "version-1",
  pending_content_version_id: null,
  has_pending_review: false,
  body: "# 原始正文\n",
  summary: "确定性摘要",
  suggested_tags: [],
  confirmed_tags: ["已确认标签"],
  collections: ["阅读合集"],
  source_metadata: {
    source_type: "markdown",
    title: "可编辑知识",
    segments: [{ locator: { kind: "markdown", path: "Notes/item.md" } }],
  },
  version_no: 1,
  note_relative_path: "Notes/item.md",
  sync_state: "synced",
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
};

function responseWith(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": "knowledge-test" }),
    json: async () => body,
  };
}

function mockKnowledge(options: { patchStatus?: number } = {}) {
  let detail = { ...baseItem };
  return vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    if (url.startsWith("/api/items") && method === "GET" && url.includes("?")) {
      return responseWith([detail]);
    }
    if (url === "/api/items/item-1" && method === "GET") {
      return responseWith(detail);
    }
    if (url === "/api/obsidian/status" && method === "GET") {
      return responseWith({ configured: true, watcher_running: true, managed_directory: "知流台" });
    }
    if (url === "/api/items/item-1" && method === "PATCH") {
      if (options.patchStatus) {
        return responseWith(
          { error: { code: "content_conflict", message: "Obsidian 内容已变化" } },
          options.patchStatus,
        );
      }
      const payload = JSON.parse(String(init.body)) as Record<string, string>;
      detail = { ...detail, title: payload.title, body: payload.body, content_hash: "b".repeat(64), version_no: 2 };
      return responseWith(detail);
    }
    if (url === "/api/items/item-1/reprocess" && method === "POST") {
      return responseWith({ item_id: "item-1", job_id: "job-reprocess", deduplicated: false });
    }
    if (url === "/api/items/item-1" && method === "DELETE") {
      return responseWith(undefined, 204);
    }
    if (url === "/api/obsidian/rescan" && method === "POST") {
      return responseWith({ changed: 1, conflicts: 0 });
    }
    throw new Error("unhandled mock route: " + method + " " + url);
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("知识库页面", () => {
  test("加载真实列表/详情、应用来源与日期筛选并展示安全来源信息", async () => {
    const fetchMock = mockKnowledge();
    vi.stubGlobal("fetch", fetchMock);
    render(<KnowledgePage />);

    expect(await screen.findByRole("heading", { level: 1, name: "知识库" })).toBeTruthy();
    expect(await screen.findByRole("heading", { level: 2, name: "可编辑知识" })).toBeTruthy();
    expect(screen.getByText("已确认标签")).toBeTruthy();
    expect(screen.queryByText("Notes/item.md")).toBeNull();
    expect(screen.getByText("受管理笔记")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("筛选标签"), { target: { value: "已确认标签" } });
    fireEvent.change(screen.getByLabelText("起始日期"), { target: { value: "2026-08-01" } });
    fireEvent.submit(screen.getByRole("button", { name: "应用筛选" }).closest("form")!);

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("tag=%E5%B7%B2%E7%A1%AE%E8%AE%A4%E6%A0%87%E7%AD%BE"))).toBe(true);
    });
  });

  test("保存 Markdown 会发送 expected_content_hash，并显示冲突而不泄漏服务细节", async () => {
    const fetchMock = mockKnowledge();
    vi.stubGlobal("fetch", fetchMock);
    render(<KnowledgePage />);
    await screen.findByRole("heading", { level: 2, name: "可编辑知识" });

    fireEvent.change(screen.getByLabelText("标题"), { target: { value: "更新后的知识" } });
    fireEvent.change(screen.getByLabelText("Markdown 正文"), { target: { value: "# 更新正文" } });
    fireEvent.submit(screen.getByLabelText("Markdown 正文").closest("form")!);
    expect((await screen.findByRole("status")).textContent).toContain("Markdown 已保存");
    const patchCall = fetchMock.mock.calls.find(([url, init]) => url === "/api/items/item-1" && init?.method === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toMatchObject({
      title: "更新后的知识",
      body: "# 更新正文",
      expected_content_hash: "a".repeat(64),
    });

    cleanup();
    vi.unstubAllGlobals();
    const conflictFetch = mockKnowledge({ patchStatus: 409 });
    vi.stubGlobal("fetch", conflictFetch);
    render(<KnowledgePage />);
    await screen.findByRole("heading", { level: 2, name: "可编辑知识" });
    fireEvent.change(screen.getByLabelText("Markdown 正文"), { target: { value: "# 冲突正文" } });
    fireEvent.submit(screen.getByLabelText("Markdown 正文").closest("form")!);
    expect((await screen.findByRole("alert")).textContent).toContain("Obsidian 内容已变化");
    expect(screen.queryByText("服务内部错误")).toBeNull();
  });

  test("重处理与软删除都调用真实服务并保留明确确认", async () => {
    const fetchMock = mockKnowledge();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<KnowledgePage />);
    await screen.findByRole("heading", { level: 2, name: "可编辑知识" });

    fireEvent.click(screen.getByRole("button", { name: "重处理" }));
    expect((await screen.findByRole("status")).textContent).toContain("已提交重处理");
    await waitFor(() => expect(screen.getByRole("button", { name: "软删除" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "软删除" }));
    expect((await screen.findByRole("status")).textContent).toContain("软删除");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/items/item-1/reprocess",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/items/item-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
