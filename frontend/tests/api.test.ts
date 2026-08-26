import { afterEach, expect, test, vi } from "vitest";

import { ApiError, requestJson } from "../src/services/api";

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
