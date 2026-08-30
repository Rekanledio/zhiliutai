import { afterEach, expect, test, vi } from "vitest";

import {
  ApiError,
  cancelJob,
  getCollection,
  getItem,
  getJob,
  getVideoCitation,
  openObsidian,
  openArtifact,
  publishItem,
  requestJson,
  retryJob,
  reviewItem,
  streamChat,
  submitVideo,
} from "../src/services/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("请求超时会中止 fetch 并返回统一错误", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      }),
    ),
  );
  await expect(requestJson("/api/slow", {}, 5)).rejects.toMatchObject<
    Partial<ApiError>
  >({ code: "request_timeout" });
});

test("外部 AbortController 可取消请求", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      }),
    ),
  );
  const controller = new AbortController();
  const pending = requestJson("/api/cancel", { signal: controller.signal });
  controller.abort();
  await expect(pending).rejects.toMatchObject<Partial<ApiError>>({
    code: "request_cancelled",
  });
});

test("Artifact target performs HEAD check and opens a PDF page fragment", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      expect(init.method).toBe("HEAD");
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
      };
    }),
  );
  const open = vi.spyOn(window, "open").mockReturnValue({} as Window);

  await openArtifact("artifact-1", 2);

  expect(open).toHaveBeenCalledWith(
    "/api/artifacts/artifact-1#page=2",
    "_blank",
    "noopener,noreferrer",
  );
  open.mockRestore();
});

test("Artifact target reports unavailable and blocked destinations", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: false,
      status: 404,
      headers: new Headers(),
      json: async () => ({
        error: { code: "artifact_not_found", message: "来源文件不可访问" },
      }),
    })),
  );
  await expect(openArtifact("artifact-1", 2)).rejects.toMatchObject<Partial<ApiError>>({
    code: "artifact_not_found",
  });

  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
    })),
  );
  const open = vi.spyOn(window, "open").mockReturnValue(null);
  await expect(openArtifact("artifact-1", 2)).rejects.toMatchObject<Partial<ApiError>>({
    code: "target_blocked",
  });
  open.mockRestore();
});

test("Artifact HEAD check obeys a strict timeout and external AbortSignal", async () => {
  const fetchMock = vi.fn((_url: string, init: RequestInit) =>
    new Promise((_resolve, reject) => {
      init.signal?.addEventListener(
        "abort",
        () => reject(new DOMException("aborted", "AbortError")),
        { once: true },
      );
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  await expect(openArtifact("artifact-1", undefined, { timeoutMs: 5 })).rejects.toMatchObject<
    Partial<ApiError>
  >({ code: "request_timeout" });

  const external = new AbortController();
  const pending = openArtifact("artifact-1", undefined, {
    signal: external.signal,
    timeoutMs: 1_000,
  });
  external.abort();
  await expect(pending).rejects.toMatchObject<Partial<ApiError>>({
    code: "request_cancelled",
  });
});

test("视频 citation 使用本机 locator API 返回字幕和关键帧定位", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/artifacts/transcript-1/locator?start_ms=120&end_ms=900") {
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          kind: "transcript",
          artifact_id: "transcript-1",
          start_ms: 120,
          end_ms: 900,
          language: "zh-Hans",
          text: "合成字幕",
          segments: [{ start_ms: 120, end_ms: 900, text: "合成字幕" }],
        }),
      };
    }
    if (
      url ===
      "/api/artifacts/frame-1/locator?start_ms=1000&end_ms=2000&keyframe_id=frame-1"
    ) {
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          kind: "keyframe",
          artifact_id: "frame-1",
          start_ms: 1000,
          end_ms: 2000,
          keyframe_id: "frame-1",
          media_type: "image/webp",
        }),
      };
    }
    throw new Error("unhandled route " + url);
  });
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    getVideoCitation("transcript-1", { startMs: 120, endMs: 900 }),
  ).resolves.toMatchObject({ kind: "transcript", text: "合成字幕" });
  await expect(
    getVideoCitation("frame-1", {
      startMs: 1000,
      endMs: 2000,
      keyframeId: "frame-1",
    }),
  ).resolves.toMatchObject({ kind: "keyframe", keyframe_id: "frame-1" });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("视频 citation locator 对不存在或被拒绝的 Artifact 保持安全错误", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: false,
      status: 404,
      headers: new Headers(),
      json: async () => ({
        error: { code: "artifact_not_found", message: "来源 Artifact 不存在" },
      }),
    })),
  );
  await expect(
    getVideoCitation("missing-1", { startMs: 0, endMs: 1000 }),
  ).rejects.toMatchObject<Partial<ApiError>>({ code: "artifact_not_found" });
});

test("SSE parser rejects citations before meta and delta", async () => {
  const frames =
    "event: citations\ndata: {\"citations\":[]}\n\n" +
    "event: meta\ndata: {}\n\n";
  const bytes = new TextEncoder().encode(frames);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ "Content-Type": "text/event-stream" }),
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(bytes);
          controller.close();
        },
      }),
    })),
  );

  await expect(
    streamChat("合成问题", { onEvent: vi.fn(), timeoutMs: 1_000 }),
  ).rejects.toMatchObject<Partial<ApiError>>({ code: "invalid_stream_order" });
});

test("SSE parser accepts both LF and CRLF frame delimiters", async () => {
  const events: string[] = [];
  const frames = [
    'event: meta\r\ndata: {"evidence":{"status":"sufficient"}}',
    'event: delta\r\ndata: {"text":"合成回答","citation_ids":[]}',
    'event: citations\r\ndata: {"citations":[]}',
    'event: done\r\ndata: {"answer":"合成回答","conflicts":[]}',
  ].join("\r\n\r\n") + "\r\n\r\n";
  const bytes = new TextEncoder().encode(frames);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ "Content-Type": "text/event-stream" }),
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(bytes);
          controller.close();
        },
      }),
    })),
  );

  await streamChat("合成问题", {
    timeoutMs: 1_000,
    onEvent: (event) => events.push(event.event),
  });
  expect(events).toEqual(["meta", "delta", "citations", "done"]);
});

test("动态资源 ID 在所有主要 API 路径中都进行 URL 编码", async () => {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({}),
      };
    }),
  );

  await getJob("job/id");
  await retryJob("retry/id");
  await cancelJob("cancel/id");
  await getItem("item/id");
  await reviewItem("review/id");
  await publishItem("publish/id");
  await openObsidian("obsidian/id");
  await getCollection("collection/id");

  expect(urls).toEqual([
    "/api/jobs/job%2Fid",
    "/api/jobs/retry%2Fid/retry",
    "/api/jobs/cancel%2Fid/cancel",
    "/api/items/item%2Fid",
    "/api/items/review%2Fid/review",
    "/api/items/publish%2Fid/publish",
    "/api/obsidian/open/obsidian%2Fid",
    "/api/collections/collection%2Fid",
  ]);
});

test("视频提交只发送允许的来源字段", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe("/api/sources/video");
      expect(init.method).toBe("POST");
      const body = JSON.parse(String(init.body));
      expect(body).toEqual({
        url: "https://video.example/watch",
        title: "合成视频",
        language: "zh-Hans",
        enable_vision: true,
      });
      return {
        ok: true,
        status: 202,
        headers: new Headers(),
        json: async () => ({ item_id: "item-1", job_id: "job-1", deduplicated: false }),
      };
    }),
  );
  await expect(
    submitVideo("https://video.example/watch", {
      title: "合成视频",
      language: "zh-Hans",
      enableVision: true,
    }),
  ).resolves.toMatchObject({ job_id: "job-1" });
});

test("任务取消调用本机取消端点", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe("/api/jobs/job-1/cancel");
      expect(init.method).toBe("POST");
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({ id: "job-1", state: "cancelled" }),
      };
    }),
  );
  await expect(cancelJob("job-1")).resolves.toMatchObject({ state: "cancelled" });
});
