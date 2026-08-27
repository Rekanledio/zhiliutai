import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
