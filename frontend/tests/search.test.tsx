import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { SearchPage } from "../src/features/search/SearchPage";

const citation = {
  citation_id: "C1",
  chunk_id: "chunk-1",
  knowledge_item_id: "item-1",
  content_version_id: "version-1",
  item_title: "SQLite 证据",
  version_no: 1,
  source_type: "markdown",
  excerpt: "SQLite 是当前版本的权威校验来源。",
  chunk_content_hash: "a".repeat(64),
  locator_status: "fallback",
  locator: { kind: "obsidian", path: "Notes/sqlite.md" },
  target: { kind: "obsidian", item_id: "item-1" },
  retrieval: {
    matched_by: ["fts"],
    fts_rank: 1,
    fts_score: 0.1,
    rrf_score: 0.016,
  },
};

function response(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "X-Request-ID": "search-test" }),
    json: async () => body,
  };
}

function streamResponse() {
  const frames = [
    "event: meta\ndata: " +
      JSON.stringify({
        query: "SQLite 证据",
        normalized_query: "SQLite 证据",
        evidence: { status: "sufficient", reason: "命中 FTS" },
        diagnostics: {
          original_query: "SQLite 证据",
          normalized_query: "SQLite 证据",
          fts_query: '"SQLite" OR "证据"',
          fts_available: true,
          vector_available: false,
          degraded: true,
          channel_errors: {},
        },
        rewrite_query: null,
        rewrite_status: "off",
        refusal: null,
      }) +
      "\n\n",
    "event: delta\ndata: " +
      JSON.stringify({
        text: "SQLite 是当前版本的权威校验来源。",
        citation_ids: ["C1"],
      }) +
      "\n\n",
    "event: citations\ndata: " +
      JSON.stringify({ citations: [citation] }) +
      "\n\n",
    "event: done\ndata: " +
      JSON.stringify({
        answer: "SQLite 是当前版本的权威校验来源。",
        conflicts: [],
        model_run_id: "run-1",
      }) +
      "\n\n",
  ].join("");
  const bytes = new TextEncoder().encode(frames);
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "Content-Type": "text/event-stream" }),
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 37));
        controller.enqueue(bytes.slice(37));
        controller.close();
      },
    }),
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("SearchPage displays structured search results and streamed citations", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/search") {
        return response({
          query: "SQLite 证据",
          normalized_query: "SQLite 证据",
          results: [
            {
              chunk_id: "chunk-1",
              knowledge_item_id: "item-1",
              content_version_id: "version-1",
              item_title: "SQLite 证据",
              version_no: 1,
              source_type: "markdown",
              excerpt: citation.excerpt,
              citation,
            },
          ],
          evidence: { status: "sufficient", reason: "命中 FTS" },
          diagnostics: {
            original_query: "SQLite 证据",
            normalized_query: "SQLite 证据",
            fts_query: '"SQLite" OR "证据"',
            fts_available: true,
            vector_available: false,
            degraded: true,
            channel_errors: {},
          },
          searched_at: "2026-08-27T00:00:00Z",
        });
      }
      if (url === "/api/chat/stream") {
        return streamResponse();
      }
      throw new Error("unhandled route " + url);
    }),
  );
  render(<SearchPage />);
  fireEvent.change(screen.getByRole("textbox", { name: "搜索问题或关键词" }), {
    target: { value: "SQLite 证据" },
  });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  expect(await screen.findByText("SQLite 证据")).toBeTruthy();
  expect(screen.getAllByText("回退定位").length).toBeGreaterThan(0);

  fireEvent.click(screen.getByRole("button", { name: "基于证据回答" }));
  expect(await screen.findByText("SQLite 是当前版本的权威校验来源。")).toBeTruthy();
  await waitFor(() => expect(screen.getAllByText("C1").length).toBeGreaterThan(0));
  expect(screen.getAllByText("回退定位").length).toBeGreaterThan(0);
});

test("SearchPage opens a PDF citation with its page locator", async () => {
  const pdfCitation = {
    ...citation,
    source_type: "pdf",
    excerpt: "第二页内容",
    locator_status: "exact",
    locator: { kind: "pdf", page: 2, page_label: "2" },
    target: { kind: "artifact", artifact_id: "artifact-1" },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/search") {
        return response({
          query: "PDF 证据",
          normalized_query: "PDF 证据",
          results: [
            {
              chunk_id: "chunk-1",
              knowledge_item_id: "item-1",
              content_version_id: "version-1",
              item_title: "PDF 证据",
              version_no: 1,
              source_type: "pdf",
              excerpt: "第二页内容",
              citation: pdfCitation,
            },
          ],
          evidence: { status: "sufficient", reason: "命中 FTS" },
          diagnostics: {
            original_query: "PDF 证据",
            normalized_query: "PDF 证据",
            fts_query: '"PDF" OR "证据"',
            fts_available: true,
            vector_available: false,
            degraded: true,
            channel_errors: {},
          },
          searched_at: "2026-08-27T00:00:00Z",
        });
      }
      if (url === "/api/artifacts/artifact-1" && init?.method === "HEAD") {
        return { ok: true, status: 200, headers: new Headers() };
      }
      throw new Error("unhandled route " + url);
    }),
  );
  const open = vi.spyOn(window, "open").mockReturnValue({} as Window);
  render(<SearchPage />);
  fireEvent.change(screen.getByRole("textbox", { name: "搜索问题或关键词" }), {
    target: { value: "PDF 证据" },
  });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  expect(await screen.findByText("第二页内容")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "打开原始文件 ↗" }));
  await waitFor(() =>
    expect(open).toHaveBeenCalledWith(
      "/api/artifacts/artifact-1#page=2",
      "_blank",
      "noopener,noreferrer",
    ),
  );
  open.mockRestore();
});

test("SearchPage locates a video transcript timestamp inside the app", async () => {
  const videoCitation = {
    ...citation,
    source_type: "video",
    excerpt: "字幕时间点证据",
    locator_status: "exact",
    locator: { kind: "video", start_ms: 120, end_ms: 900, language: "zh-Hans" },
    target: {
      kind: "artifact",
      artifact_id: "transcript-1",
      start_ms: 120,
      end_ms: 900,
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/search") {
        return response({
          query: "字幕时间点证据",
          normalized_query: "字幕时间点证据",
          results: [{ ...videoCitation, citation: videoCitation }],
          evidence: { status: "sufficient", reason: "命中字幕" },
          diagnostics: {
            original_query: "字幕时间点证据",
            normalized_query: "字幕时间点证据",
            fts_query: '"字幕时间点证据"',
            fts_available: true,
            vector_available: false,
            degraded: true,
            channel_errors: {},
          },
          searched_at: "2026-08-27T00:00:00Z",
        });
      }
      if (url === "/api/artifacts/transcript-1/locator?start_ms=120&end_ms=900") {
        return response({
          kind: "transcript",
          artifact_id: "transcript-1",
          start_ms: 120,
          end_ms: 900,
          language: "zh-Hans",
          text: "时间点字幕高亮",
          segments: [{ start_ms: 120, end_ms: 900, text: "时间点字幕高亮" }],
        });
      }
      throw new Error("unhandled route " + url);
    }),
  );
  render(<SearchPage />);
  fireEvent.change(screen.getByRole("textbox", { name: "搜索问题或关键词" }), {
    target: { value: "字幕时间点证据" },
  });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  expect(await screen.findByText("字幕时间点证据")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "在应用内定位" }));
  expect(await screen.findByText("时间点字幕高亮")).toBeTruthy();
  expect(screen.getByRole("region", { name: "视频证据定位" })).toBeTruthy();
});

test("SearchPage reports a blocked keyframe Artifact without opening a media URL", async () => {
  const keyframeCitation = {
    ...citation,
    source_type: "video",
    excerpt: "关键帧证据",
    locator_status: "exact",
    locator: { kind: "video_keyframe", start_ms: 1000, end_ms: 2000, keyframe_ids: ["frame-1"] },
    target: {
      kind: "artifact",
      artifact_id: "frame-1",
      start_ms: 1000,
      end_ms: 2000,
      keyframe_id: "frame-1",
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/search") {
        return response({
          query: "关键帧证据",
          normalized_query: "关键帧证据",
          results: [{ ...keyframeCitation, citation: keyframeCitation }],
          evidence: { status: "sufficient", reason: "命中视觉证据" },
          diagnostics: {
            original_query: "关键帧证据",
            normalized_query: "关键帧证据",
            fts_query: '"关键帧证据"',
            fts_available: true,
            vector_available: false,
            degraded: true,
            channel_errors: {},
          },
          searched_at: "2026-08-27T00:00:00Z",
        });
      }
      if (
        url ===
        "/api/artifacts/frame-1/locator?start_ms=1000&end_ms=2000&keyframe_id=frame-1"
      ) {
        return response({
          kind: "keyframe",
          artifact_id: "frame-1",
          start_ms: 1000,
          end_ms: 2000,
          keyframe_id: "frame-1",
          media_type: "image/webp",
        });
      }
      throw new Error("unhandled route " + url);
    }),
  );
  render(<SearchPage />);
  fireEvent.change(screen.getByRole("textbox", { name: "搜索问题或关键词" }), {
    target: { value: "关键帧证据" },
  });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  expect(await screen.findByText("关键帧证据")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "在应用内定位" }));
  const image = await screen.findByAltText("关键帧 frame-1");
  fireEvent.error(image);
  expect(await screen.findByText("关键帧 Artifact 不可访问")).toBeTruthy();
});
