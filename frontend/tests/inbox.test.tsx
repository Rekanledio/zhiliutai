import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import App from "../src/app/App";
import { InboxPage } from "../src/features/inbox/InboxPage";
import { requestJson, submitUrl } from "../src/services/api";

const dashboardResponse = {
  greeting: "测试问候",
  date_label: "8 月 30 日 · 星期日",
  stats: { knowledge_count: 0, today_added: 0, pending_review: 0, processing: 0 },
  health: {
    status: "healthy",
    checked_at: "2026-08-30T00:00:00Z",
    components: [],
  },
  pending_reviews: [],
  recent_items: [],
  processing_jobs: [],
};

function responseWith(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": "inbox-test" }),
    json: async () => body,
  };
}

function emptyInboxFetch() {
  return vi.fn(async (url: string) => {
    if (url === "/api/dashboard") return responseWith(dashboardResponse);
    if (url === "/api/items") return responseWith([]);
    throw new Error("unhandled route " + url);
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("批次 A 统一采集", () => {
  test.each([
    ["粘贴文本", "文本"],
    ["上传文件", "文件"],
    ["添加网页", "网页"],
    ["添加视频", "视频"],
  ])("首页 %s 入口导航并预选 %s 模式", async (entry, mode) => {
    vi.stubGlobal("fetch", emptyInboxFetch());
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "测试问候，先整理一点点" });

    fireEvent.click(screen.getByRole("button", { name: new RegExp(entry) }));

    expect(await screen.findByRole("heading", { level: 1, name: "收件箱" })).toBeTruthy();
    expect(screen.getByRole("button", { name: mode }).getAttribute("aria-pressed")).toBe("true");
  });

  test("FormData 请求不自动添加 JSON Content-Type", async () => {
    let received: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        received = init;
        return responseWith({ ok: true });
      }),
    );
    const form = new FormData();
    form.append("file", new File(["内容"], "note.md", { type: "text/markdown" }));

    await requestJson("/api/sources/files", { method: "POST", body: form });

    expect(received?.body).toBe(form);
    expect(received?.headers).toEqual(
      expect.objectContaining({ Accept: "application/json" }),
    );
    expect(received?.headers).not.toHaveProperty("Content-Type");
  });

  test("网页模式调用既有 URL service 并保留允许字段", async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/url") {
        expect(init.method).toBe("POST");
        expect(JSON.parse(String(init.body))).toEqual({
          url: "https://example.com/article",
          title: "静态文章",
        });
        return responseWith({ item_id: "item-1", job_id: "job-1", deduplicated: true }, 202);
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="url" />);

    fireEvent.change(screen.getByRole("textbox", { name: "网页 URL" }), {
      target: { value: "https://example.com/article" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "网页标题" }), {
      target: { value: "静态文章" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交网页" }));

    expect((await screen.findByRole("status")).textContent).toContain("内容已去重");
  });

  test("文件按 MD/TXT 文本和 PDF/DOCX multipart 分流", async () => {
    const textRequests: Array<Record<string, unknown>> = [];
    const fileRequests: File[] = [];
    let jobNumber = 0;
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/text") {
        textRequests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        jobNumber += 1;
        return responseWith({ item_id: "item-" + jobNumber, job_id: "job-" + jobNumber, deduplicated: false }, 202);
      }
      if (url === "/api/sources/files") {
        const body = init.body as FormData;
        fileRequests.push(body.get("file") as File);
        jobNumber += 1;
        return responseWith({ item_id: "item-" + jobNumber, job_id: "job-" + jobNumber, deduplicated: false }, 202);
      }
      if (url.startsWith("/api/jobs/job-")) {
        return responseWith({ state: "succeeded" });
      }
      throw new Error("unhandled route " + (init.method ?? "GET") + " " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="file" />);

    const files = [
      new File(["# Markdown"], "note.md", { type: "text/markdown" }),
      new File(["纯文本"], "plain.txt", { type: "text/plain" }),
      new File(["%PDF"], "paper.pdf", { type: "application/pdf" }),
      new File(["PK"], "report.docx", {
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      }),
    ];
    fireEvent.change(screen.getByLabelText("选择文件"), { target: { files } });
    fireEvent.click(screen.getByRole("button", { name: "提交文件批次" }));

    expect((await screen.findByRole("status")).textContent).toContain("批次完成：4/4 项已提交或去重");
    expect(textRequests.map((request) => request.source_type)).toEqual(["markdown", "text"]);
    expect(fileRequests.map((file) => file.name)).toEqual(["paper.pdf", "report.docx"]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sources/files",
      expect.objectContaining({
        method: "POST",
        headers: expect.not.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
  });

  test("非法、空文件和超大文件给出稳定反馈", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/items") return responseWith([]);
      throw new Error("unhandled route " + url);
    }));
    render(<InboxPage onChanged={vi.fn()} initialMode="file" />);

    const tooLarge = new File([new Uint8Array(10_000_001)], "large.txt", {
      type: "text/plain",
    });
    fireEvent.change(screen.getByLabelText("选择文件"), {
      target: {
        files: [
          new File(["x"], "unknown.exe", { type: "application/octet-stream" }),
          new File([], "empty.txt", { type: "text/plain" }),
          tooLarge,
        ],
      },
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("unknown.exe：不支持的文件类型");
    expect(alert.textContent).toContain("empty.txt：文件为空");
    expect(alert.textContent).toContain("large.txt：文件超过 10 MB");
    expect(screen.queryByText("提交文件批次")).toBeTruthy();
  });

  test("批量提交一项失败不会丢失其他项目", async () => {
    let sourceNumber = 0;
    const submittedBodies: string[] = [];
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/text") {
        sourceNumber += 1;
        submittedBodies.push(String(init.body));
        if (sourceNumber === 1) {
          return responseWith(
            { error: { code: "synthetic_failure", message: "合成失败" } },
            500,
          );
        }
        return responseWith({ item_id: "item-2", job_id: "job-2", deduplicated: false }, 202);
      }
      if (url === "/api/jobs/job-2") return responseWith({ state: "succeeded" });
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="file" />);
    fireEvent.change(screen.getByLabelText("选择文件"), {
      target: {
        files: [
          new File(["first"], "first.txt", { type: "text/plain" }),
          new File(["second"], "second.txt", { type: "text/plain" }),
        ],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交文件批次" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("批次完成：1/2 项成功");
    expect(alert.textContent).toContain("first.txt");
    expect(submittedBodies).toHaveLength(2);
    expect(JSON.parse(submittedBodies[1]).content).toBe("second");
  });

  test("取消文件提交请求会中止尚未返回 job_id 的请求并保留队列", async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/text") {
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
            once: true,
          });
        });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="file" />);
    fireEvent.change(screen.getByLabelText("选择文件"), {
      target: {
        files: [new File(["pending"], "pending.txt", { type: "text/plain" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交文件批次" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消提交" }));

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toContain("提交请求已停止");
    });
    expect(screen.getByText("pending.txt")).toBeTruthy();
    expect(screen.getByText("提交已停止（待确认）")).toBeTruthy();
  });

  test("拖放文件进入队列并提示重复选择", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/items") return responseWith([]);
      throw new Error("unhandled route " + url);
    }));
    render(<InboxPage onChanged={vi.fn()} initialMode="file" />);
    const file = new File(["拖放内容"], "dropped.md", { type: "text/markdown" });
    const dropZone = screen.getByRole("button", { name: "拖放文件或按 Enter 选择" });

    fireEvent.drop(dropZone, { dataTransfer: { files: [file] } });
    expect(await screen.findByText("dropped.md")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("选择文件"), { target: { files: [file] } });

    expect((await screen.findByRole("alert")).textContent).toContain("已在当前批次中");
  });

  test("视频模式只提交允许的 URL 参数", async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/video") {
        expect(JSON.parse(String(init.body))).toEqual({
          url: "https://example.com/video",
          title: "演示视频",
          language: "zh-Hans",
          enable_vision: true,
        });
        return responseWith({ item_id: "item-video", job_id: "job-video", deduplicated: true }, 202);
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="video" />);

    fireEvent.change(await screen.findByRole("textbox", { name: "视频 URL" }), {
      target: { value: "https://example.com/video" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "视频标题" }), {
      target: { value: "演示视频" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "字幕语言" }), {
      target: { value: "zh-Hans" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "启用条件视觉处理" }));
    fireEvent.click(screen.getByRole("button", { name: "提交视频" }));

    expect((await screen.findByRole("status")).textContent).toContain("内容已去重");
  });

  test("非文件采集取消提交后显示待确认而不是伪装成后台已取消", async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/url") {
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="url" />);

    fireEvent.change(await screen.findByRole("textbox", { name: "网页 URL" }), {
      target: { value: "https://example.com/slow" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交网页" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消提交" }));

    expect((await screen.findByRole("status")).textContent).toContain("提交请求已停止");
    expect((await screen.findByRole("status")).textContent).toContain("任务状态待确认");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("已获得 job_id 后取消会调用现有后台任务取消端点", async () => {
    let cancelCalls = 0;
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/url") {
        return responseWith({ item_id: "item-1", job_id: "job-1", deduplicated: false }, 202);
      }
      if (url === "/api/jobs/job-1") return responseWith({ state: "queued" });
      if (url === "/api/jobs/job-1/cancel") {
        cancelCalls += 1;
        expect(init.method).toBe("POST");
        return responseWith({ id: "job-1", state: "cancelled" });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="url" />);

    fireEvent.change(await screen.findByRole("textbox", { name: "网页 URL" }), {
      target: { value: "https://example.com/cancellable" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交网页" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消后台任务" }));

    expect((await screen.findByRole("status")).textContent).toContain("后台任务已取消");
    expect(cancelCalls).toBe(1);
    expect(screen.queryByText("提交已停止")).toBeNull();
  });

  test("停止等待只停止前台轮询，保留 job_id 且不调用取消端点", async () => {
    let cancelCalls = 0;
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/url") {
        return responseWith({ item_id: "item-1", job_id: "job-1", deduplicated: false }, 202);
      }
      if (url === "/api/jobs/job-1") return responseWith({ state: "running" });
      if (url === "/api/jobs/job-1/cancel") {
        cancelCalls += 1;
        return responseWith({ id: "job-1", state: "cancelled" });
      }
      throw new Error("unhandled route " + (init.method ?? "GET") + " " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="url" />);

    fireEvent.change(await screen.findByRole("textbox", { name: "网页 URL" }), {
      target: { value: "https://example.com/background" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交网页" }));
    fireEvent.click(await screen.findByRole("button", { name: "停止等待（后台继续）" }));

    expect((await screen.findByRole("status")).textContent).toContain("仍在后台处理");
    expect(cancelCalls).toBe(0);
    expect(screen.getByRole("button", { name: "取消后台任务" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "已有后台任务" }) as HTMLButtonElement).disabled).toBe(true);
  });

  test("queued/running 超过 15 秒等待窗口后保留后台任务且不重复提交", async () => {
    vi.useFakeTimers();
    let sourceCalls = 0;
    let cancelCalls = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/items") return responseWith([]);
      if (url === "/api/sources/url") {
        sourceCalls += 1;
        return responseWith({ item_id: "item-1", job_id: "job-1", deduplicated: false }, 202);
      }
      if (url === "/api/jobs/job-1") return responseWith({ state: "running" });
      if (url === "/api/jobs/job-1/cancel") {
        cancelCalls += 1;
        return responseWith({ id: "job-1", state: "cancelled" });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="url" />);

    fireEvent.change(screen.getByRole("textbox", { name: "网页 URL" }), {
      target: { value: "https://example.com/slow-background" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交网页" }));
    await act(async () => {
      for (let index = 0; index < 20; index += 1) {
        await Promise.resolve();
      }
    });
    expect(screen.getByRole("button", { name: "取消后台任务" })).toBeTruthy();
    await vi.advanceTimersByTimeAsync(15_200);
    await act(async () => {
      for (let index = 0; index < 20; index += 1) {
        await Promise.resolve();
      }
    });

    expect((screen.getByRole("status")).textContent).toContain("仍在后台处理");
    expect(screen.getByRole("button", { name: "取消后台任务" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "已有后台任务" }) as HTMLButtonElement).disabled).toBe(true);
    expect(sourceCalls).toBe(1);
    expect(cancelCalls).toBe(0);
  });

  test("审核详情展示并提交用户编辑的摘要标签和合集", async () => {
    let reviewed = false;
    let published = false;
    const pending = {
      id: "item-review",
      title: "AI 草稿标题",
      source_type: "markdown",
      status: "pending_review",
      content_hash: "a".repeat(64),
      current_content_version_id: "version-review",
      pending_content_version_id: null,
      has_pending_review: true,
      body: "草稿正文\n",
      summary: "AI 草稿摘要",
      suggested_tags: ["AI"],
      suggested_collections: ["待读合集"],
      confirmed_tags: [],
      collections: [],
      created_at: "2026-08-30T00:00:00Z",
      updated_at: "2026-08-30T00:00:00Z",
    };
    const approved = {
      ...pending,
      title: "确认标题",
      status: "reviewed",
      body: "确认正文\n",
      summary: "确认摘要",
      suggested_tags: ["确认标签"],
      suggested_collections: ["确认合集"],
    };
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/items") return responseWith(published ? [] : [reviewed ? approved : pending]);
      if (url === "/api/items/item-review") {
        return responseWith(published ? { ...approved, status: "published", has_pending_review: false } : reviewed ? approved : pending);
      }
      if (url === "/api/items/item-review/review") {
        expect(JSON.parse(String(init.body))).toEqual({
          decision: "approve",
          title: "确认标题",
          body: "确认正文",
          summary: "确认摘要",
          suggested_tags: ["确认标签"],
          suggested_collections: ["确认合集"],
        });
        reviewed = true;
        return responseWith(approved);
      }
      if (url === "/api/items/item-review/publish") {
        expect(init.method).toBe("POST");
        published = true;
        return responseWith({ ...approved, status: "published", has_pending_review: false });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InboxPage onChanged={vi.fn()} initialMode="text" />);

    expect(await screen.findByText("AI 草稿标题")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看审核详情" }));
    expect(await screen.findByRole("complementary", { name: "审核详情" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "审核标题" }), {
      target: { value: "确认标题" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "审核正文" }), {
      target: { value: "确认正文" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "AI 摘要" }), {
      target: { value: "确认摘要" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "建议标签" }), {
      target: { value: "确认标签" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "建议合集" }), {
      target: { value: "确认合集" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认审核" }));

    expect((await screen.findByRole("status")).textContent).toContain("审核已确认");
    expect(screen.getAllByRole("button", { name: "发布到 Obsidian" })).toHaveLength(2);
    expect((screen.getByRole("textbox", { name: "审核标题" }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("textbox", { name: "审核正文" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect((await screen.findByRole("note")).textContent).toContain("需要先返回待审核状态后才能再次修改");

    fireEvent.change(screen.getByRole("textbox", { name: "审核标题" }), {
      target: { value: "不应静默保存的修改" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "发布到 Obsidian" })[1]);

    expect((await screen.findByRole("status")).textContent).toContain("已发布到 Obsidian");
    expect(published).toBe(true);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/items/item-review/review")).toHaveLength(1);
  });

});
