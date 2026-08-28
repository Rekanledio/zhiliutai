import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  type KnowledgeItem,
  getItem,
  getItems,
  getJob,
  publishItem,
  reviewItem,
  submitVideo,
  submitText,
} from "../../services/api";

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.message : "操作失败，请稍后重试";
}

export function InboxPage({ onChanged }: { onChanged: () => void }) {
  const [content, setContent] = useState("");
  const [sourceType, setSourceType] = useState<"text" | "markdown">("text");
  const [captureMode, setCaptureMode] = useState<"text" | "markdown" | "video">("text");
  const [videoUrl, setVideoUrl] = useState("");
  const [videoTitle, setVideoTitle] = useState("");
  const [videoLanguage, setVideoLanguage] = useState("");
  const [videoVision, setVideoVision] = useState(false);
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [jobState, setJobState] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const loadItems = useCallback(async (signal?: AbortSignal) => {
    const next = await getItems(undefined, signal);
    setItems(
      next.filter(
        (item) =>
          ["pending_review", "reviewed"].includes(item.status) || item.has_pending_review,
      ),
    );
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadItems(controller.signal).catch((reason) => {
      if (!(reason instanceof ApiError && reason.code === "request_cancelled")) {
        setError(messageFor(reason));
      }
    });
    return () => controller.abort();
  }, [loadItems]);

  const pollJob = async (jobId: string, signal: AbortSignal) => {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const job = await getJob(jobId, signal);
      setJobState(job.state);
      if (job.state === "succeeded") {
        return;
      }
      if (job.state === "failed" || job.state === "cancelled") {
        throw new ApiError(
          typeof job.error?.message === "string" ? job.error.message : "处理任务失败",
          null,
          "job_failed",
          null,
        );
      }
      await new Promise((resolve) => window.setTimeout(resolve, 120));
    }
    throw new ApiError("等待任务完成超时", null, "job_wait_timeout", null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (captureMode === "video" ? !videoUrl.trim() : !content.trim()) {
      setError(captureMode === "video" ? "请输入视频 URL" : "请输入文本或 Markdown");
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError(null);
    setJobState("queued");
    try {
      const submitted =
        captureMode === "video"
          ? await submitVideo(videoUrl, {
              title: videoTitle,
              language: videoLanguage,
              enableVision: videoVision,
              signal: controller.signal,
            })
          : await submitText(content, sourceType, controller.signal);
      if (!submitted.deduplicated) {
        await pollJob(submitted.job_id, controller.signal);
      }
      await loadItems(controller.signal);
      setContent("");
      setVideoUrl("");
      setVideoTitle("");
      setVideoLanguage("");
      onChanged();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
      }
      setBusy(false);
    }
  };

  const act = async (item: KnowledgeItem, action: "review" | "publish") => {
    setError(null);
    try {
      const updated =
        action === "review" ? await reviewItem(item.id) : await publishItem(item.id);
      const detail = await getItem(updated.id);
      setItems((current) =>
        current
          .map((entry) => (entry.id === detail.id ? detail : entry))
          .filter((entry) => entry.status !== "published" || entry.has_pending_review),
      );
      onChanged();
    } catch (reason) {
      setError(messageFor(reason));
    }
  };

  return (
    <section className="stage-page inbox-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">Text / Markdown / Video</span>
          <h1>收件箱</h1>
          <p>输入先形成草稿，经你审核后才写入 Obsidian；视频先采集字幕，未确认内容不会进入索引。</p>
        </div>
        <span className="local-badge">本机持久化任务</span>
      </div>
      <form className="capture-form" onSubmit={(event) => void submit(event)}>
          <div className="format-switch" aria-label="内容格式">
            <button
              className={captureMode === "video" ? "is-selected" : ""}
              type="button"
              onClick={() => setCaptureMode("video")}
            >
              视频
            </button>
            {(["text", "markdown"] as const).map((format) => (
              <button
                className={captureMode === format ? "is-selected" : ""}
                key={format}
                type="button"
                onClick={() => {
                  setCaptureMode(format);
                  setSourceType(format);
                }}
            >
              {format === "text" ? "纯文本" : "Markdown"}
            </button>
          ))}
        </div>
        {captureMode === "video" ? (
          <div className="video-capture-fields">
            <label>
              视频 URL
              <input
                aria-label="视频 URL"
                type="url"
                value={videoUrl}
                onChange={(event) => setVideoUrl(event.target.value)}
                placeholder="https://…"
              />
            </label>
            <label>
              标题（可选）
              <input
                aria-label="视频标题"
                value={videoTitle}
                onChange={(event) => setVideoTitle(event.target.value)}
              />
            </label>
            <label>
              语言（可选）
              <input
                aria-label="字幕语言"
                value={videoLanguage}
                onChange={(event) => setVideoLanguage(event.target.value)}
                placeholder="zh-Hans"
              />
            </label>
            <label className="checkbox-row">
              <input
                aria-label="启用条件视觉处理"
                type="checkbox"
                checked={videoVision}
                onChange={(event) => setVideoVision(event.target.checked)}
              />
              对幻灯片/教程尝试条件视觉处理
            </label>
          </div>
        ) : (
          <textarea
            aria-label="知识内容"
            value={content}
            onChange={(event) => setContent(event.target.value)}
            placeholder="粘贴一段想法、摘录或 Markdown…"
            rows={10}
          />
        )}
        <div className="form-actions">
          <span>{jobState ? "任务状态：" + jobState : "视频采集只接受 URL，不读取本地路径或浏览器凭据"}</span>
          {busy ? (
            <button
              className="ghost-button"
              type="button"
              onClick={() => controllerRef.current?.abort()}
            >
              取消等待
            </button>
          ) : null}
          <button className="primary-button" disabled={busy} type="submit">
            {busy ? "处理中…" : "提交到收件箱"}
          </button>
        </div>
      </form>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      <div className="review-list">
        <div className="section-heading compact">
          <div><span className="eyebrow">人工确认</span><h2>待处理草稿</h2></div>
        </div>
        {items.length === 0 ? (
          <p className="quiet-note">当前没有待处理草稿。</p>
        ) : (
          items.map((item) => (
            <article className="review-item" key={item.id}>
              <div>
                <strong>{item.title}</strong>
                <span>{item.source_type} · {item.status}</span>
              </div>
              <button
                className={item.status === "reviewed" ? "primary-button" : "ghost-button"}
                type="button"
                onClick={() =>
                  void act(
                    item,
                    item.status === "reviewed" ||
                      (item.status === "published" && Boolean(item.has_pending_review))
                      ? "publish"
                      : "review",
                  )
                }
              >
                {item.status === "reviewed" ||
                (item.status === "published" && Boolean(item.has_pending_review))
                  ? "发布到 Obsidian"
                  : "审核通过"}
              </button>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
