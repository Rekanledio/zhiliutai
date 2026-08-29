import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { CollectionsPage } from "../src/features/collections/CollectionsPage";

const member = {
  id: "item-1",
  title: "已有知识",
  source_type: "markdown",
  version_no: 2,
  suggested_tags: ["阅读"],
};

const addableItem = {
  id: "item-2",
  title: "可加入知识",
  source_type: "text",
  status: "published",
  content_hash: "b".repeat(64),
  current_content_version_id: "version-2",
  suggested_tags: [],
  version_no: 1,
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

const draftItem = {
  ...addableItem,
  id: "item-draft",
  title: "待审核知识",
  status: "pending_review",
};

function responseWith(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": "collection-test" }),
    json: async () => body,
  };
}

function mockCollections() {
  let detail = {
    id: "collection-1",
    name: "人工合集",
    description: "用于测试",
    item_count: 1,
    moc_enabled: false,
    moc_status: "not_enabled" as const,
    items: [member],
    related_tags: ["阅读"],
  };
  const summary = () => ({
    id: detail.id,
    name: detail.name,
    description: detail.description,
    item_count: detail.items.length,
    moc_enabled: false,
  });
  return vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    if (url === "/api/collections" && method === "GET") {
      return responseWith([summary()]);
    }
    if (url === "/api/items?status=published" && method === "GET") {
      return responseWith([addableItem, draftItem]);
    }
    if (url === "/api/collections/collection-1" && method === "GET") {
      return responseWith(detail);
    }
    if (url === "/api/collections/collection-1" && method === "PATCH") {
      const payload = JSON.parse(String(init.body)) as {
        name: string;
        description: string | null;
      };
      detail = { ...detail, ...payload };
      return responseWith(detail);
    }
    if (url === "/api/collections/collection-1/items/item-2" && method === "POST") {
      detail = {
        ...detail,
        item_count: 2,
        items: [...detail.items, { ...addableItem, status: undefined }],
        related_tags: ["阅读"],
      };
      return responseWith(detail);
    }
    if (url === "/api/collections/collection-1/items/item-2" && method === "DELETE") {
      detail = { ...detail, item_count: 1, items: [member] };
      return responseWith(detail);
    }
    if (url === "/api/collections/collection-1" && method === "DELETE") {
      return responseWith(undefined, 204);
    }
    throw new Error("unhandled mock route: " + method + " " + url);
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("合集页面", () => {
  test("创建边界、编辑、过滤已发布条目、加入移除与删除确认", async () => {
    const fetchMock = mockCollections();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<CollectionsPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "合集" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "人工合集" })).toBeTruthy();
    expect(screen.getByText("MOC：未启用")).toBeTruthy();
    expect(screen.getByText("成员变更会同步到受管理 Markdown 的 collections")).toBeTruthy();
    expect(screen.queryByRole("option", { name: "已有知识" })).toBeNull();
    expect(screen.getByRole("option", { name: "可加入知识" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "待审核知识" })).toBeNull();

    fireEvent.change(screen.getByLabelText("合集说明"), { target: { value: "更新说明" } });
    fireEvent.submit(screen.getByLabelText("合集说明").closest("form")!);
    expect((await screen.findByRole("status")).textContent).toContain("合集信息已保存");

    fireEvent.change(screen.getByLabelText("选择已发布知识条目"), {
      target: { value: "item-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    expect(await screen.findByText("可加入知识")).toBeTruthy();
    const addedRow = screen.getByText("可加入知识").closest("article")!;
    fireEvent.click(within(addedRow).getByRole("button", { name: "移除" }));
    await waitFor(() => {
      expect(screen.getAllByText("可加入知识").length).toBe(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "删除合集" }));
    await waitFor(() => expect(screen.getByText("从一个人工合集开始")).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/collections/collection-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  test("加载错误只显示脱敏的通用消息", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("SECRET_PATH")));
    render(<CollectionsPage />);
    expect((await screen.findByRole("alert")).textContent).toContain("无法连接本机 API");
    expect(screen.queryByText("SECRET_PATH")).toBeNull();
  });

  test("可以创建人工合集", async () => {
    const created = {
      id: "collection-new",
      name: "新人工合集",
      description: "新合集说明",
      item_count: 0,
      moc_enabled: false,
      moc_status: "not_enabled" as const,
      items: [],
      related_tags: [],
    };
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      if (url === "/api/collections" && method === "GET") {
        return responseWith([]);
      }
      if (url === "/api/items?status=published" && method === "GET") {
        return responseWith([]);
      }
      if (url === "/api/collections" && method === "POST") {
        return responseWith(created, 201);
      }
      throw new Error("unhandled mock route: " + method + " " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CollectionsPage />);

    fireEvent.change(await screen.findByLabelText("新合集名称"), {
      target: { value: "新人工合集" },
    });
    fireEvent.change(screen.getByLabelText("说明（可选）"), {
      target: { value: "新合集说明" },
    });
    fireEvent.submit(screen.getByLabelText("新合集名称").closest("form")!);

    expect((await screen.findByRole("status")).textContent).toContain("合集已创建");
    expect(screen.getByRole("heading", { level: 2, name: "新人工合集" })).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/collections",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "新人工合集", description: "新合集说明" }),
      }),
    );
  });
});
