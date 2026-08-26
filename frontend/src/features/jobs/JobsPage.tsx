import { useEffect, useState } from "react";

import { ApiError, type ProcessingJob, getJobs, retryJob } from "../../services/api";

export function JobsPage() {
  const [jobs, setJobs] = useState<ProcessingJob[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => setJobs(await getJobs());

  useEffect(() => {
    void refresh().catch((reason) =>
      setError(reason instanceof ApiError ? reason.message : "任务列表加载失败"),
    );
  }, []);

  const retry = async (id: string) => {
    try {
      await retryJob(id);
      await refresh();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "重试失败");
    }
  };

  return (
    <section className="stage-page">
      <div className="stage-heading">
        <div><span className="eyebrow">SQLite JobRunner</span><h1>处理任务</h1><p>状态、重试和错误在进程重启后仍会保留。</p></div>
        <button className="ghost-button" type="button" onClick={() => void refresh()}>刷新</button>
      </div>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      <div className="knowledge-list">
        {jobs.length === 0 ? <p className="quiet-note">当前没有任务。</p> : jobs.map((job) => (
          <article className="knowledge-row" key={job.id}>
            <div><strong>{job.kind}</strong><span>{job.state} · {job.stage} · 重试 {job.retry_count}/{job.max_retries}</span></div>
            {job.state === "failed" ? <button className="ghost-button" type="button" onClick={() => void retry(job.id)}>重试</button> : null}
          </article>
        ))}
      </div>
    </section>
  );
}
