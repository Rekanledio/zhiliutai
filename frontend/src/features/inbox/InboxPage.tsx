import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, FormEvent, KeyboardEvent } from "react";

import {
  ApiError,
  type KnowledgeItem,
  cancelJob,
  getItem,
  getItems,
  getJob,
  publishItem,
  reviewItem,
  submitFile,
  submitText,
  submitUrl,
  submitVideo,
} from "../../services/api";

export type InboxCaptureMode = "text" | "file" | "url" | "video";
type TextSourceType = "text" | "markdown";
type FileKind = "text" | "markdown" | "pdf" | "docx";
type FileQueueStatus =
  | "queued"
  | "processing"
  | "succeeded"
  | "deduplicated"
  | "failed"
  | "cancelled"
  | "background"
  | "submission_cancelled";

interface QueuedFile {
  key: string;
  file: File;
  displayName: string;
  kind: FileKind;
  status: FileQueueStatus;
  message?: string;
}

interface BatchProgress {
  current: number;
  total: number;
  name: string;
}

interface ReviewForm {
  title: string;
  body: string;
  summary: string;
  suggestedTags: string;
  suggestedCollections: string;
}

const MAX_SOURCE_BYTES = 10_000_000;

const fileRules: Record<
  string,
  { kind: FileKind; label: string; mimeTypes: readonly string[] }
> = {
  ".md": { kind: "markdown", label: "Markdown", mimeTypes: ["text/markdown", "text/plain"] },
  ".markdown": {
    kind: "markdown",
    label: "Markdown",
    mimeTypes: ["text/markdown", "text/plain"],
  },
  ".txt": { kind: "text", label: "TXT", mimeTypes: ["text/plain"] },
  ".pdf": { kind: "pdf", label: "PDF", mimeTypes: ["application/pdf"] },
  ".docx": {
    kind: "docx",
    label: "DOCX",
    mimeTypes: [
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
  },
};

const fileStatusLabels: Record<FileQueueStatus, string> = {
  queued: "待提交",
  processing: "处理中",
  succeeded: "已提交",
  deduplicated: "已去重",
  failed: "失败",
  cancelled: "已取消",
  background: "后台处理中",
  submission_cancelled: "提交已停止（待确认）",
};

const jobStateLabels: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  deduplicated: "已去重",
  background: "后台处理中",
  submission_cancelled: "提交已停止",
};

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.message : "操作失败，请稍后重试";
}

function isCancelled(error: unknown): boolean {
  return error instanceof ApiError && error.code === "request_cancelled";
}

function cancelledError(): ApiError {
  return new ApiError("请求已取消", null, "request_cancelled", null);
}

function safeFilename(value: string): string {
  const basename = value.split(/[\\/]/).pop() ?? "";
  const cleaned = basename.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  return cleaned || "未命名文件";
}

function fileTitle(filename: string): string | undefined {
  const title = filename.replace(/\.[^.]+$/, "").trim();
  return title ? title.slice(0, 300) : undefined;
}

function fileKey(file: File, displayName: string): string {
  return displayName + ":" + file.size + ":" + file.lastModified;
}

function reviewFormFor(item: KnowledgeItem): ReviewForm {
  return {
    title: item.title,
    body: item.body ?? "",
    summary: item.summary ?? "",
    suggestedTags: item.suggested_tags.join("、"),
    suggestedCollections: (item.suggested_collections ?? []).join("、"),
  };
}

function splitReviewNames(value: string): string[] {
  return value
    .split(/[、,\n]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

async function readFileText(file: File): Promise<string> {
  if (typeof file.text === "function") {
    return file.text();
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new ApiError("文件读取失败", null, "file_read_failed", null));
    reader.readAsText(file);
  });
}

function validateFile(file: File): { displayName: string; kind: FileKind } | string {
  const displayName = safeFilename(file.name);
  const extension = displayName.slice(displayName.lastIndexOf(".")).toLowerCase();
  const rule = fileRules[extension];
  if (!rule) {
    return displayName + "：不支持的文件类型，请选择 MD、TXT、PDF 或 DOCX";
  }
  if (file.size === 0) {
    return displayName + "：文件为空，无法提交";
  }
  if (file.size > MAX_SOURCE_BYTES) {
    return displayName + "：文件超过 10 MB 大小限制";
  }
  const mimeType = file.type.trim().toLowerCase();
  if (mimeType && !rule.mimeTypes.includes(mimeType)) {
    return displayName + "：文件 MIME 类型与扩展名不匹配";
  }
  return { displayName, kind: rule.kind };
}

function waitWithSignal(signal: AbortSignal, milliseconds: number): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(cancelledError());
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, milliseconds);
    const cancel = () => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      reject(cancelledError());
    };
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export function InboxPage({
  onChanged,
  initialMode = "text",
  onNavigate,
}: {
  onChanged: () => void;
  initialMode?: InboxCaptureMode;
  onNavigate?: (page: "jobs") => void;
}) {
  const [content, setContent] = useState("");
  const [sourceType, setSourceType] = useState<TextSourceType>("text");
  const [captureMode, setCaptureMode] = useState<InboxCaptureMode>(initialMode);
  const [url, setUrl] = useState("");
  const [urlTitle, setUrlTitle] = useState("");
  const [videoUrl, setVideoUrl] = useState("");
  const [videoTitle, setVideoTitle] = useState("");
  const [videoLanguage, setVideoLanguage] = useState("");
  const [videoVision, setVideoVision] = useState(false);
  const [files, setFiles] = useState<QueuedFile[]>([]);
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [jobState, setJobState] = useState<string | null>(null);
  const [batchProgress, setBatchProgress] = useState<BatchProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [reviewingItem, setReviewingItem] = useState<KnowledgeItem | null>(null);
  const [reviewForm, setReviewForm] = useState<ReviewForm | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const activeJobRef = useRef<string | null>(null);
  const activeFileKeyRef = useRef<string | null>(null);
  const cancelIntentRef = useRef<"submission" | "task" | "wait" | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [cancellingJobId, setCancellingJobId] = useState<string | null>(null);

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

  const trackJob = (jobId: string | null, fileKey: string | null = null) => {
    activeJobRef.current = jobId;
    activeFileKeyRef.current = fileKey;
    setActiveJobId(jobId);
  };

  const clearTrackedJob = (jobId: string) => {
    if (activeJobRef.current === jobId) {
      trackJob(null);
    }
  };

  const pollJob = async (jobId: string, signal: AbortSignal) => {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const job = await getJob(jobId, signal);
      setJobState(job.state);
      if (job.state === "succeeded") {
        clearTrackedJob(jobId);
        return;
      }
      if (job.state === "failed" || job.state === "cancelled") {
        clearTrackedJob(jobId);
        throw new ApiError(
          job.state === "cancelled"
            ? "后台任务已取消"
            : typeof job.error?.message === "string"
              ? job.error.message
              : "处理任务失败",
          null,
          job.state === "cancelled" ? "job_cancelled" : "job_failed",
          null,
        );
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        break;
      }
      await waitWithSignal(signal, Math.min(120, remaining));
    }
    throw new ApiError("等待任务完成超时", null, "job_wait_timeout", null);
  };

  const updateFile = (key: string, updates: Partial<QueuedFile>) => {
    setFiles((current) =>
      current.map((entry) => (entry.key === key ? { ...entry, ...updates } : entry)),
    );
  };

  const stopWaiting = () => {
    const jobId = activeJobRef.current;
    if (!jobId) {
      cancelIntentRef.current = "submission";
      controllerRef.current?.abort();
      return;
    }
    cancelIntentRef.current = "wait";
    setJobState("background");
    const fileKey = activeFileKeyRef.current;
    if (fileKey) {
      updateFile(fileKey, {
        status: "background",
        message: "已停止等待，任务仍在后台处理",
      });
    }
    setMessage("已停止前台等待，任务仍在后台处理（" + jobId + "）；未重复提交。");
    controllerRef.current?.abort();
  };

  const cancelBackgroundTask = async () => {
    const jobId = activeJobRef.current;
    if (!jobId || cancellingJobId) {
      return;
    }
    cancelIntentRef.current = "task";
    setCancellingJobId(jobId);
    controllerRef.current?.abort();
    try {
      const cancelled = await cancelJob(jobId);
      const fileKey = activeFileKeyRef.current;
      if (cancelled.state === "cancelled") {
        setJobState("cancelled");
        if (fileKey) {
          updateFile(fileKey, { status: "cancelled", message: "后台任务已取消" });
        }
        trackJob(null);
        setMessage("后台任务已取消，未完成内容不会继续处理。");
      } else if (cancelled.state === "succeeded") {
        setJobState("succeeded");
        if (fileKey) {
          updateFile(fileKey, { status: "succeeded", message: "任务已在取消前完成" });
        }
        trackJob(null);
        setMessage("任务已在取消前完成。");
      } else if (cancelled.state === "failed") {
        setJobState("failed");
        if (fileKey) {
          updateFile(fileKey, { status: "failed", message: "任务已失败" });
        }
        trackJob(null);
        setError("任务已失败，请查看处理任务中的错误摘要。");
      } else {
        setJobState("background");
        setError("取消请求未生效，任务仍在后台处理，请进入处理任务查看。");
      }
    } catch (reason) {
      setJobState("background");
      const fileKey = activeFileKeyRef.current;
      if (fileKey) {
        updateFile(fileKey, {
          status: "background",
          message: "取消失败，任务仍可能在后台处理",
        });
      }
      setError("后台任务取消失败，任务仍可能继续运行；请进入处理任务重试。" + " " + messageFor(reason));
    } finally {
      setCancellingJobId(null);
      cancelIntentRef.current = null;
    }
  };

  const cancelSubmission = () => {
    if (activeJobRef.current) {
      void cancelBackgroundTask();
      return;
    }
    cancelIntentRef.current = "submission";
    setMessage("正在取消提交请求…");
    controllerRef.current?.abort();
  };

  const addFiles = (incoming: File[]) => {
    const additions: QueuedFile[] = [];
    const notices: string[] = [];
    const seen = new Set(files.map((entry) => entry.key));
    for (const file of incoming) {
      const validation = validateFile(file);
      if (typeof validation === "string") {
        notices.push(validation);
        continue;
      }
      const key = fileKey(file, validation.displayName);
      if (seen.has(key)) {
        notices.push(validation.displayName + "：已在当前批次中，已跳过重复选择");
        continue;
      }
      seen.add(key);
      additions.push({
        key,
        file,
        displayName: validation.displayName,
        kind: validation.kind,
        status: "queued",
      });
    }
    if (additions.length > 0) {
      setFiles((current) => [...current, ...additions]);
      setMessage(additions.length + " 个文件已加入待提交批次");
    } else if (!notices.length) {
      setMessage(null);
    }
    setError(notices.length ? notices.join("；") : null);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    addFiles(Array.from(event.dataTransfer.files));
  };

  const handleDropKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInputRef.current?.click();
    }
  };

  const submitFileEntry = async (entry: QueuedFile, signal: AbortSignal) => {
    const title = fileTitle(entry.displayName);
    if (entry.kind === "markdown" || entry.kind === "text") {
      const text = await readFileText(entry.file);
      return submitText(text, entry.kind, { title, signal });
    }
    return submitFile(entry.file, { title, signal });
  };

  const submitFileBatch = async (signal: AbortSignal) => {
    const pending = files.filter((entry) =>
      ["queued", "failed", "cancelled"].includes(entry.status),
    );
    if (!pending.length) {
      setError("请先选择至少一个有效文件");
      return;
    }
    const failures: string[] = [];
    let completed = 0;
    let cancelled = false;
    let stoppedWaiting = false;
    let submissionStopped = false;
    let taskCancellationInProgress = false;
    for (const [index, entry] of pending.entries()) {
      if (signal.aborted) {
        cancelled = true;
        break;
      }
      setBatchProgress({ current: index + 1, total: pending.length, name: entry.displayName });
      updateFile(entry.key, { status: "processing", message: undefined });
      let trackedJobId: string | null = null;
      try {
        const submitted = await submitFileEntry(entry, signal);
        if (submitted.deduplicated) {
          setJobState("deduplicated");
          updateFile(entry.key, { status: "deduplicated" });
        } else {
          trackedJobId = submitted.job_id;
          trackJob(submitted.job_id, entry.key);
          setJobState("queued");
          await pollJob(submitted.job_id, signal);
          updateFile(entry.key, { status: "succeeded" });
        }
        completed += 1;
        try {
          await loadItems(signal);
          onChanged();
        } catch (refreshError) {
          if (!isCancelled(refreshError)) {
            setError(messageFor(refreshError));
          }
        }
      } catch (reason) {
        const cancelIntent = cancelIntentRef.current;
        if (cancelIntent === "wait") {
          stoppedWaiting = true;
          break;
        }
        if (cancelIntent === "task") {
          taskCancellationInProgress = true;
          break;
        }
        if (cancelIntent === "submission") {
          updateFile(entry.key, {
            status: "submission_cancelled",
            message: "提交请求已停止，任务状态待确认",
          });
          submissionStopped = true;
          break;
        }
        if (reason instanceof ApiError && reason.code === "job_cancelled") {
          updateFile(entry.key, { status: "cancelled", message: "后台任务已取消" });
          cancelled = true;
          break;
        }
        if (
          trackedJobId &&
          activeJobRef.current === trackedJobId &&
          !(reason instanceof ApiError && reason.code === "job_failed")
        ) {
          stoppedWaiting = true;
          updateFile(entry.key, {
            status: "background",
            message: "暂时无法取得任务状态，任务仍可能在后台处理",
          });
          break;
        }
        if (isCancelled(reason) || signal.aborted) {
          updateFile(entry.key, { status: "cancelled", message: "本批次已取消" });
          cancelled = true;
          break;
        }
        const detail = messageFor(reason);
        updateFile(entry.key, { status: "failed", message: detail });
        failures.push(entry.displayName + "：" + detail);
      }
    }
    setBatchProgress(null);
    if (stoppedWaiting) {
      setJobState("background");
      setMessage("已停止前台等待，当前任务仍在后台处理；未重复提交，可进入处理任务查看。");
      return;
    }
    if (taskCancellationInProgress) {
      return;
    }
    if (submissionStopped) {
      setJobState("submission_cancelled");
      setMessage("提交请求已停止；尚未收到任务 ID，任务状态待确认，未重复提交。");
      return;
    }
    if (cancelled) {
      setMessage(
        "批次已取消，已完成 " + completed + "/" + pending.length + " 项；未处理项目仍保留在队列。",
      );
      return;
    }
    if (failures.length) {
      setError(
        "批次完成：" + completed + "/" + pending.length + " 项成功。" + failures.join("；"),
      );
    } else {
      setMessage("批次完成：" + completed + "/" + pending.length + " 项已提交或去重");
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (activeJobRef.current) {
      setError("已有任务仍在后台处理，请先取消或进入处理任务查看；本次未重复提交。");
      return;
    }
    if (captureMode === "file") {
      if (!files.some((entry) => ["queued", "failed", "cancelled"].includes(entry.status))) {
        setError("请先选择至少一个有效文件");
        return;
      }
    } else if (captureMode === "video" ? !videoUrl.trim() : captureMode === "url" ? !url.trim() : !content.trim()) {
      setError(
        captureMode === "video"
          ? "请输入视频 URL"
          : captureMode === "url"
            ? "请输入网页 URL"
            : "请输入文本或 Markdown",
      );
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    cancelIntentRef.current = null;
    setBusy(true);
    setError(null);
    setMessage(null);
    if (captureMode !== "file") {
      setJobState("queued");
    }
    try {
      if (captureMode === "file") {
        await submitFileBatch(controller.signal);
        return;
      }
      const submitted =
        captureMode === "video"
          ? await submitVideo(videoUrl.trim(), {
              title: videoTitle,
              language: videoLanguage,
              enableVision: videoVision,
              signal: controller.signal,
            })
          : captureMode === "url"
            ? await submitUrl(url.trim(), {
                title: urlTitle.trim() || undefined,
                signal: controller.signal,
              })
            : await submitText(content, sourceType, { signal: controller.signal });
      if (!submitted.deduplicated) {
        trackJob(submitted.job_id);
        setJobState("queued");
        await pollJob(submitted.job_id, controller.signal);
        setJobState("succeeded");
        setMessage("已提交，草稿已进入人工确认队列");
      } else {
        setJobState("deduplicated");
        setMessage("内容已去重，沿用已有处理记录");
      }
      await loadItems(controller.signal);
      setContent("");
      setUrl("");
      setUrlTitle("");
      setVideoUrl("");
      setVideoTitle("");
      setVideoLanguage("");
      onChanged();
    } catch (reason) {
      const cancelIntent = cancelIntentRef.current;
      if (reason instanceof ApiError && reason.code === "job_wait_timeout") {
        const jobId = activeJobRef.current;
        setJobState("background");
        setMessage(
          "任务仍在后台处理" +
            (jobId ? "（" + jobId + "）" : "") +
            "，已停止前台等待；未重复提交，可进入处理任务查看。",
        );
      } else if (cancelIntent === "wait") {
        setJobState("background");
        setMessage("已停止前台等待，任务仍在后台处理；未重复提交，可进入处理任务查看。");
      } else if (cancelIntent === "task") {
        // cancelBackgroundTask owns the final UI state after the backend
        // confirms whether the task really stopped.
        return;
      } else if (cancelIntent === "submission" || isCancelled(reason) || controller.signal.aborted) {
        setJobState("submission_cancelled");
        setMessage("提交请求已停止；尚未收到任务 ID，任务状态待确认，未重复提交。");
      } else {
        setError(messageFor(reason));
      }
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

  const openReview = async (item: KnowledgeItem) => {
    setError(null);
    try {
      const detail = await getItem(item.id);
      setReviewingItem(detail);
      setReviewForm(reviewFormFor(detail));
    } catch (reason) {
      setError(messageFor(reason));
    }
  };

  const submitReviewAction = async (
    action: "approve" | "reject" | "cancel" | "publish" | "reject_publish" | "cancel_publish",
  ) => {
    if (!reviewingItem || !reviewForm || reviewBusy) {
      return;
    }
    setReviewBusy(true);
    setError(null);
    try {
      const updated =
        action === "publish"
          ? await publishItem(reviewingItem.id)
          : action === "reject_publish" || action === "cancel_publish"
            ? await publishItem(reviewingItem.id, action === "reject_publish" ? "reject" : "cancel")
            : await reviewItem(reviewingItem.id, {
                decision: action,
                ...(action === "approve"
                  ? {
                      title: reviewForm.title.trim(),
                      body: reviewForm.body,
                      summary: reviewForm.summary,
                      suggestedTags: splitReviewNames(reviewForm.suggestedTags),
                      suggestedCollections: splitReviewNames(reviewForm.suggestedCollections),
                    }
                  : {}),
              });
      const detail = await getItem(updated.id);
      await loadItems();
      onChanged();
      if (action === "approve" || action === "publish") {
        setReviewingItem(detail);
        setReviewForm(reviewFormFor(detail));
      } else {
        setReviewingItem(null);
        setReviewForm(null);
      }
      setMessage(
        action === "approve"
          ? "审核已确认，等待发布确认。"
          : action === "publish"
            ? "已发布到 Obsidian。"
            : action === "cancel" || action === "cancel_publish"
              ? "已取消当前审核操作。"
              : "已拒绝当前候选版本。",
      );
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setReviewBusy(false);
    }
  };

  const modeOptions: Array<{ mode: InboxCaptureMode; label: string; hint: string }> = [
    { mode: "text", label: "文本", hint: "粘贴文本或 Markdown" },
    { mode: "file", label: "文件", hint: "MD、TXT、PDF、DOCX" },
    { mode: "url", label: "网页", hint: "静态网页 URL" },
    { mode: "video", label: "视频", hint: "视频 URL" },
  ];

  return (
    <section className="stage-page inbox-page">
      <div className="stage-heading">
        <div>
          <span className="eyebrow">Text / Markdown / File / URL</span>
          <h1>收件箱</h1>
          <p>输入先形成草稿，经你审核后才写入 Obsidian；未确认内容不会进入索引。</p>
        </div>
        <span className="local-badge">本机持久化任务</span>
      </div>
      <form className="capture-form" onSubmit={(event) => void submit(event)}>
        <div className="format-switch capture-mode-switch" aria-label="采集模式" role="group">
          {modeOptions.map((option) => (
            <button
              aria-pressed={captureMode === option.mode}
              className={captureMode === option.mode ? "is-selected" : ""}
              disabled={busy}
              key={option.mode}
              type="button"
              onClick={() => setCaptureMode(option.mode)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <p className="capture-mode-hint">
          {modeOptions.find((option) => option.mode === captureMode)?.hint}
        </p>
        {captureMode === "text" ? (
          <div className="text-capture-fields">
            <div className="format-switch" aria-label="文本格式" role="group">
              {(["text", "markdown"] as const).map((format) => (
                <button
                  aria-pressed={sourceType === format}
                  className={sourceType === format ? "is-selected" : ""}
                  disabled={busy}
                  key={format}
                  type="button"
                  onClick={() => setSourceType(format)}
                >
                  {format === "text" ? "纯文本" : "Markdown"}
                </button>
              ))}
            </div>
            <textarea
              aria-label="知识内容"
              disabled={busy}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              placeholder="粘贴一段想法、摘录或 Markdown…"
              rows={10}
            />
          </div>
        ) : captureMode === "file" ? (
          <div className="file-capture-fields">
            <label className="file-select-label" htmlFor="source-file-input">
              选择文件
            </label>
            <input
              ref={fileInputRef}
              id="source-file-input"
              className="sr-only"
              type="file"
              accept=".md,.markdown,.txt,.pdf,.docx,text/markdown,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              multiple
              onChange={handleFileChange}
            />
            <div
              aria-label="拖放文件或按 Enter 选择"
              className={"file-drop-zone" + (dragActive ? " is-dragging" : "")}
              role="button"
              tabIndex={0}
              onClick={() => fileInputRef.current?.click()}
              onDragEnter={(event) => {
                event.preventDefault();
                setDragActive(true);
              }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={() => setDragActive(false)}
              onDrop={handleDrop}
              onKeyDown={handleDropKeyDown}
            >
              <strong>拖放文件到这里</strong>
              <span>也可以按 Enter 或点击“选择文件” · 单文件不超过 10 MB</span>
            </div>
            {files.length ? (
              <ul className="file-queue" aria-label="待提交文件" aria-live="polite">
                {files.map((entry) => (
                  <li className="file-queue-item" key={entry.key}>
                    <span className="file-queue-name">{entry.displayName}</span>
                    <span
                      className={
                        "file-queue-status" + (entry.status === "failed" ? " is-error" : "")
                      }
                    >
                      {fileStatusLabels[entry.status]}
                    </span>
                    {entry.message ? <small>{entry.message}</small> : null}
                    {!busy &&
                    entry.status !== "processing" &&
                    entry.status !== "background" &&
                    entry.status !== "submission_cancelled" ? (
                      <button
                        aria-label={"移除 " + entry.displayName}
                        className="file-queue-remove"
                        type="button"
                        onClick={() => setFiles((current) => current.filter((item) => item.key !== entry.key))}
                      >
                        ×
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="file-queue-empty">尚未选择文件。</p>
            )}
          </div>
        ) : captureMode === "url" ? (
          <div className="url-capture-fields">
            <label>
              网页 URL
              <input
                aria-label="网页 URL"
                disabled={busy}
                type="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://…"
              />
            </label>
            <label>
              标题（可选）
              <input
                aria-label="网页标题"
                disabled={busy}
                value={urlTitle}
                onChange={(event) => setUrlTitle(event.target.value)}
              />
            </label>
            <p>仅提交 URL；后端继续负责静态网页获取、SSRF、DNS、重定向、大小和超时校验。</p>
          </div>
        ) : (
          <div className="video-capture-fields">
            <label>
              视频 URL
              <input
                aria-label="视频 URL"
                disabled={busy}
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
                disabled={busy}
                value={videoTitle}
                onChange={(event) => setVideoTitle(event.target.value)}
              />
            </label>
            <label>
              语言（可选）
              <input
                aria-label="字幕语言"
                disabled={busy}
                value={videoLanguage}
                onChange={(event) => setVideoLanguage(event.target.value)}
                placeholder="zh-Hans"
              />
            </label>
            <label className="checkbox-row">
              <input
                aria-label="启用条件视觉处理"
                disabled={busy}
                type="checkbox"
                checked={videoVision}
                onChange={(event) => setVideoVision(event.target.checked)}
              />
              对幻灯片/教程尝试条件视觉处理
            </label>
          </div>
        )}
        <div className="form-actions">
          <span>
            {batchProgress
              ? "正在处理 " + batchProgress.current + "/" + batchProgress.total + "：" + batchProgress.name
            : jobState
                ? "任务状态：" + (jobStateLabels[jobState] ?? jobState)
                : captureMode === "file"
                  ? "支持 MD、TXT、PDF、DOCX；后端仍会执行最终校验"
                  : captureMode === "url"
                    ? "网页安全校验由后端 SourceFetcher 负责"
                    : "视频采集只接受 URL，不读取本地路径或浏览器凭据"}
          </span>
          {busy && !activeJobId ? (
            <button
              className="ghost-button"
              type="button"
              onClick={cancelSubmission}
            >
              取消提交
            </button>
          ) : null}
          {activeJobId ? (
            <>
              <button
                className="ghost-button"
                disabled={cancellingJobId !== null}
                type="button"
                onClick={() => void cancelBackgroundTask()}
              >
                {cancellingJobId === activeJobId ? "取消中…" : "取消后台任务"}
              </button>
              <button
                className="ghost-button"
                disabled={cancellingJobId !== null}
                type="button"
                onClick={stopWaiting}
              >
                停止等待（后台继续）
              </button>
              {onNavigate ? (
                <button
                  className="text-button"
                  disabled={cancellingJobId !== null}
                  type="button"
                  onClick={() => onNavigate("jobs")}
                >
                  查看处理任务
                </button>
              ) : null}
            </>
          ) : null}
          <button className="primary-button" disabled={busy || activeJobId !== null} type="submit">
            {busy
              ? "处理中…"
              : activeJobId
                ? "已有后台任务"
              : captureMode === "file"
                ? "提交文件批次"
                : captureMode === "url"
                  ? "提交网页"
                  : captureMode === "video"
                    ? "提交视频"
                    : "提交到收件箱"}
          </button>
        </div>
      </form>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {message ? <div className="inline-message" role="status">{message}</div> : null}
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
              <div className="review-item-actions">
                <button className="text-button" type="button" onClick={() => void openReview(item)}>
                  查看审核详情
                </button>
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
              </div>
            </article>
          ))
        )}
      </div>
      {reviewingItem && reviewForm ? (
        <aside className="review-detail" aria-label="审核详情">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">人工确认详情</span>
              <h2>{reviewingItem.title}</h2>
            </div>
            <button
              className="text-button"
              type="button"
              onClick={() => {
                setReviewingItem(null);
                setReviewForm(null);
              }}
            >
              关闭
            </button>
          </div>
          <div className="review-source-meta">
            <span>来源：{reviewingItem.source_type}</span>
            <span>状态：{reviewingItem.status}</span>
            {reviewingItem.version_no ? <span>版本：v{reviewingItem.version_no}</span> : null}
          </div>
          <div className="review-edit-grid">
            <label>
              标题
              <input
                aria-label="审核标题"
                disabled={reviewBusy || reviewingItem.status !== "pending_review"}
                value={reviewForm.title}
                onChange={(event) => setReviewForm({ ...reviewForm, title: event.target.value })}
              />
            </label>
            <label>
              AI 摘要
              <textarea
                aria-label="AI 摘要"
                disabled={reviewBusy || reviewingItem.status !== "pending_review"}
                value={reviewForm.summary}
                onChange={(event) => setReviewForm({ ...reviewForm, summary: event.target.value })}
                rows={4}
              />
            </label>
            <label className="review-edit-wide">
              正文
              <textarea
                aria-label="审核正文"
                disabled={reviewBusy || reviewingItem.status !== "pending_review"}
                value={reviewForm.body}
                onChange={(event) => setReviewForm({ ...reviewForm, body: event.target.value })}
                rows={12}
              />
            </label>
            <label>
              建议标签（逗号分隔）
              <input
                aria-label="建议标签"
                disabled={reviewBusy || reviewingItem.status !== "pending_review"}
                value={reviewForm.suggestedTags}
                onChange={(event) =>
                  setReviewForm({ ...reviewForm, suggestedTags: event.target.value })
                }
              />
            </label>
            <label>
              建议合集（逗号分隔）
              <input
                aria-label="建议合集"
                disabled={reviewBusy || reviewingItem.status !== "pending_review"}
                value={reviewForm.suggestedCollections}
                onChange={(event) =>
                  setReviewForm({ ...reviewForm, suggestedCollections: event.target.value })
                }
              />
            </label>
          </div>
          {reviewingItem.status !== "pending_review" ? (
            <p className="review-lock-note" role="note">
              审核已确认，字段已锁定；需要先返回待审核状态后才能再次修改。
            </p>
          ) : null}
          <div className="review-detail-actions">
            {reviewingItem.status === "pending_review" ? (
              <>
                <button
                  className="primary-button"
                  disabled={reviewBusy}
                  type="button"
                  onClick={() => void submitReviewAction("approve")}
                >
                  确认审核
                </button>
                <button
                  className="ghost-button"
                  disabled={reviewBusy}
                  type="button"
                  onClick={() => void submitReviewAction("reject")}
                >
                  拒绝
                </button>
                <button
                  className="ghost-button"
                  disabled={reviewBusy}
                  type="button"
                  onClick={() => void submitReviewAction("cancel")}
                >
                  取消
                </button>
              </>
            ) : null}
            {reviewingItem.status === "reviewed" ||
            (reviewingItem.status === "published" && Boolean(reviewingItem.has_pending_review)) ? (
              <>
                <button
                  className="primary-button"
                  disabled={reviewBusy}
                  type="button"
                  onClick={() => void submitReviewAction("publish")}
                >
                  发布到 Obsidian
                </button>
                <button
                  className="ghost-button"
                  disabled={reviewBusy}
                  type="button"
                  onClick={() => void submitReviewAction("reject_publish")}
                >
                  拒绝发布
                </button>
                <button
                  className="ghost-button"
                  disabled={reviewBusy}
                  type="button"
                  onClick={() => void submitReviewAction("cancel_publish")}
                >
                  取消发布
                </button>
              </>
            ) : null}
          </div>
        </aside>
      ) : null}
    </section>
  );
}
