import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  cancelJob,
  type JobAttempt,
  type ProcessingJob,
  getJobs,
  retryJob,
} from "../../services/api";

const stateLabels: Record<ProcessingJob["state"], string> = {
  queued: "排队中",
  running: "处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const attemptStateLabels: Record<JobAttempt["state"], string> = stateLabels;

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "—";
  if (durationMs < 1000) return durationMs + " ms";
  const seconds = durationMs / 1000;
  if (seconds < 60) return seconds.toFixed(1) + " 秒";
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return minutes + " 分 " + remainder + " 秒";
}

function progressValue(value: number): number {
  return Math.round(Math.max(0, Math.min(1, value)) * 100);
}

function errorMessage(error: Record<string, unknown> | null | undefined): string | null {
  const message = error?.message;
  return typeof message === "string" && message ? message : null;
}

function attemptError(attempt: JobAttempt): string | null {
  return errorMessage(attempt.error);
}

export function JobsPage() {
  const [jobs, setJobs] = useState<ProcessingJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyJobId, setBusyJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setJobs(await getJobs());
      setError(null);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "任务列表加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runAction = async (job: ProcessingJob, action: "retry" | "cancel") => {
    if (busyJobId) return;
    setBusyJobId(job.id);
    setError(null);
    setMessage(null);
    try {
      if (action === "retry") {
        await retryJob(job.id);
        setMessage("任务已重新排队");
      } else {
        await cancelJob(job.id);
        setMessage("任务已取消");
      }
      await refresh();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : action === "retry" ? "重试失败" : "取消失败");
    } finally {
      setBusyJobId(null);
    }
  };

  return (
    <section className="stage-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">SQLite JobRunner</span>
          <h1>处理任务</h1>
          <p>状态、进度、尝试记录和错误摘要在进程重启后仍会保留。</p>
        </div>
        <button className="ghost-button" type="button" onClick={() => void refresh()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {message ? <div className="inline-message" role="status">{message}</div> : null}
      {loading && jobs.length === 0 ? <p className="quiet-note">正在加载任务…</p> : null}
      {!loading && jobs.length === 0 ? <p className="quiet-note">当前没有处理任务。</p> : null}
      <div className="jobs-list">
        {jobs.map((job) => {
          const percent = progressValue(job.progress);
          const currentError = errorMessage(job.error);
          return (
            <article className="job-card" key={job.id}>
              <div className="job-card-heading">
                <div>
                  <span className="eyebrow">{job.kind}</span>
                  <h2>{stateLabels[job.state] ?? job.state}</h2>
                </div>
                <span className={"job-state job-state-" + job.state}>{job.stage}</span>
              </div>
              <div className="job-progress-row">
                <progress max="100" value={percent} aria-label={job.kind + " 进度"}>{percent}%</progress>
                <strong>{percent}%</strong>
              </div>
              <dl className="job-metadata">
                <div><dt>创建时间</dt><dd>{formatTimestamp(job.created_at)}</dd></div>
                <div><dt>开始时间</dt><dd>{formatTimestamp(job.started_at)}</dd></div>
                <div><dt>结束时间</dt><dd>{formatTimestamp(job.finished_at)}</dd></div>
                <div><dt>耗时</dt><dd>{formatDuration(job.duration_ms)}</dd></div>
                <div><dt>最后 heartbeat</dt><dd>{formatTimestamp(job.heartbeat_at)}</dd></div>
                <div><dt>重试次数</dt><dd>{job.retry_count}/{job.max_retries}</dd></div>
              </dl>
              {currentError ? <div className="job-error"><strong>错误摘要</strong><span>{currentError}</span></div> : null}
              {job.attempts?.length ? (
                <details className="job-attempts" open={job.state === "failed"}>
                  <summary>JobAttempt 历史（{job.attempts.length}）</summary>
                  <div className="job-attempt-list">
                    {job.attempts.map((attempt) => (
                      <div className="job-attempt" key={attempt.id}>
                        <div>
                          <strong>第 {attempt.attempt_no} 次 · {attemptStateLabels[attempt.state] ?? attempt.state}</strong>
                          <span>{attempt.stage}</span>
                        </div>
                        <div>
                          <span>{formatDuration(attempt.duration_ms)}</span>
                          {attemptError(attempt) ? <small>{attemptError(attempt)}</small> : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
              <div className="row-actions">
                {job.state === "failed" ? (
                  <button className="ghost-button" type="button" disabled={busyJobId !== null} onClick={() => void runAction(job, "retry")}>
                    {busyJobId === job.id ? "处理中…" : "重试"}
                  </button>
                ) : null}
                {job.state === "queued" || job.state === "running" ? (
                  <button className="ghost-button" type="button" disabled={busyJobId !== null} onClick={() => void runAction(job, "cancel")}>
                    {busyJobId === job.id ? "处理中…" : "取消"}
                  </button>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
