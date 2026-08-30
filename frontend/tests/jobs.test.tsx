import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { JobsPage } from "../src/features/jobs/JobsPage";

const baseJob = {
  id: "job-failed",
  kind: "ingest_text",
  state: "failed" as const,
  stage: "failed",
  progress: 1,
  retry_count: 0,
  max_retries: 3,
  error: { code: "job_failed", message: "已脱敏错误摘要", type: "RuntimeError" },
  result: null,
  heartbeat_at: "2026-08-30T08:02:00Z",
  created_at: "2026-08-30T08:00:00Z",
  started_at: "2026-08-30T08:00:01Z",
  finished_at: "2026-08-30T08:02:00Z",
  duration_ms: 119000,
  attempts: [
    {
      id: "attempt-1",
      attempt_no: 1,
      state: "failed" as const,
      stage: "failed",
      started_at: "2026-08-30T08:00:01Z",
      heartbeat_at: "2026-08-30T08:02:00Z",
      finished_at: "2026-08-30T08:02:00Z",
      duration_ms: 119000,
      error: { code: "job_failed", message: "已脱敏错误摘要" },
    },
  ],
};

const runningJob = {
  ...baseJob,
  id: "job-running",
  state: "running" as const,
  stage: "parsing",
  progress: 0.4,
  error: null,
  finished_at: null,
  duration_ms: 5000,
  attempts: [
    {
      ...baseJob.attempts[0],
      id: "attempt-running",
      state: "running" as const,
      stage: "parsing",
      finished_at: null,
      duration_ms: 5000,
      error: null,
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("任务页展示生命周期/进度/heartbeat/attempt，并支持重试和取消", async () => {
  let jobs = [baseJob, runningJob];
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    if (url === "/api/jobs" && (init.method ?? "GET") === "GET") return responseWith(jobs);
    if (url === "/api/jobs/job-failed/retry" && init.method === "POST") {
      jobs = [{ ...baseJob, state: "queued", stage: "queued", error: null }, runningJob];
      return responseWith(jobs[0]);
    }
    if (url === "/api/jobs/job-running/cancel" && init.method === "POST") {
      jobs = [jobs[0], { ...runningJob, state: "cancelled", stage: "cancelled", finished_at: "2026-08-30T08:03:00Z" }];
      return responseWith(jobs[1]);
    }
    throw new Error("unhandled route " + url);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<JobsPage />);
  expect(await screen.findByRole("heading", { level: 1, name: "处理任务" })).toBeTruthy();
  expect(screen.getAllByText("1 分 59 秒").length).toBeGreaterThan(0);
  expect(screen.getAllByText("最后 heartbeat").length).toBe(2);
  expect(screen.getAllByText(/JobAttempt 历史/).length).toBe(2);
  expect(screen.getAllByText("已脱敏错误摘要").length).toBe(2);
  expect(screen.queryByText("UPSTREAM_RESPONSE_SECRET")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect((await screen.findByRole("status")).textContent).toContain("任务已重新排队");
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/jobs/job-failed/retry", expect.objectContaining({ method: "POST" })));

  fireEvent.click(screen.getAllByRole("button", { name: "取消" })[1]);
  expect((await screen.findByRole("status")).textContent).toContain("任务已取消");
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/jobs/job-running/cancel", expect.objectContaining({ method: "POST" })));
});

function responseWith(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": "jobs-test" }),
    json: async () => body,
  };
}
