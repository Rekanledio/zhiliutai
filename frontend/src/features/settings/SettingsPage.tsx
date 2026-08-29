import { useEffect, useState } from "react";

import {
  ApiError,
  createBackup,
  getSettings,
  rebuildDerivedState,
  rescanSettings,
  type ProviderSettings,
  type SettingsBackupResponse,
  type SettingsHealthState,
  type SettingsRebuildResponse,
  type SettingsRescanResponse,
  type SettingsResponse,
} from "../../services/api";

const providerLabels: Record<ProviderSettings["capability"], string> = {
  chat: "Chat",
  embedding: "Embedding",
  asr: "ASR",
  vision: "Vision",
  reranker: "Reranker",
};

const stateLabels: Record<SettingsHealthState, string> = {
  healthy: "正常",
  degraded: "降级",
  not_configured: "未配置",
  configured: "已配置",
  unavailable: "不可用",
};

const providerKinds: Record<ProviderSettings["provider_kind"], string> = {
  "openai-compatible": "OpenAI-compatible",
  fastembed: "FastEmbed",
};

const sensitiveText = /(?:api[_ -]?key|authorization|bearer|cookie|set[-_ ]?cookie|token|secret|password)\s*[:=]\s*\S|bearer\s+\S|traceback|stack\s+trace/i;
const unsafeModelText = /(?:api[_ -]?key|authorization|bearer|cookie|set[-_ ]?cookie|token|secret|password|traceback|stack\s+trace)/i;
function containsAbsolutePath(value: string): boolean {
  const slash = String.fromCharCode(92);
  const boundaryCharacters = [" ", "'", "(", "[", "{", "=", ":"];
  const isBoundary = (character: string | undefined) =>
    character === undefined || boundaryCharacters.includes(character);

  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (character === "/" && isBoundary(value[index - 1])) {
      return true;
    }
    if (
      index + 2 < value.length &&
      /^[A-Za-z]$/.test(character) &&
      value[index + 1] === ":" &&
      (value[index + 2] === "/" || value[index + 2] === slash) &&
      isBoundary(value[index - 1])
    ) {
      return true;
    }
    if (
      character === slash &&
      value[index + 1] === slash &&
      isBoundary(value[index - 1])
    ) {
      return true;
    }
  }
  return false;
}

function safeText(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= 500 &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    !sensitiveText.test(value) &&
    !containsAbsolutePath(value)
  );
}

function safeRelativeDirectory(value: unknown): value is string {
  return (
    safeText(value) &&
    value.length > 0 &&
    !value.includes("\\") &&
    !value.startsWith("/") &&
    !value.split("/").some((part) => part === "" || part === "." || part === "..")
  );
}

function safeModel(value: unknown): value is string {
  return (
    safeText(value) &&
    !unsafeModelText.test(value) &&
    !value.includes("://") &&
    !value.includes("?")
  );
}

function finiteInteger(value: unknown, minimum = 0): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= minimum;
}

function finiteNumber(value: unknown, minimum = 0, maximum = 1): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function hasExactKeys(
  value: unknown,
  expectedKeys: readonly string[],
): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const actualKeys = Object.keys(value).sort();
  const sortedExpectedKeys = [...expectedKeys].sort();
  return (
    actualKeys.length === sortedExpectedKeys.length &&
    actualKeys.every((key, index) => key === sortedExpectedKeys[index])
  );
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

const rescanResponseKeys = [
  "changed",
  "renamed",
  "missing",
  "conflicts",
  "invalid",
  "deferred",
] as const;

function isSafeRescanResponse(value: unknown): value is SettingsRescanResponse {
  return (
    hasExactKeys(value, rescanResponseKeys) &&
    rescanResponseKeys.every((key) => nonNegativeInteger(value[key]))
  );
}

function isSafeRebuildResponse(value: unknown): value is SettingsRebuildResponse {
  return (
    hasExactKeys(value, ["published_items", "chunks"]) &&
    nonNegativeInteger(value.published_items) &&
    nonNegativeInteger(value.chunks)
  );
}

function isSafeBackupResponse(value: unknown): value is SettingsBackupResponse {
  return (
    hasExactKeys(value, ["archive_id", "created_at", "sha256", "config_key"]) &&
    typeof value.archive_id === "string" &&
    /^backup-[0-9a-f]{32}$/.test(value.archive_id) &&
    typeof value.created_at === "string" &&
    value.created_at.length >= 1 &&
    value.created_at.length <= 80 &&
    safeText(value.created_at) &&
    typeof value.sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(value.sha256) &&
    value.config_key === "BACKUP_ROOT"
  );
}

function isSafeSettings(value: unknown): value is SettingsResponse {
  if (!value || typeof value !== "object") {
    return false;
  }
  const settings = value as SettingsResponse;
  if (
    typeof settings.local_only !== "boolean" ||
    !["127.0.0.1", "loopback", "non_loopback"].includes(settings.bind_host) ||
    !settings.vault ||
    !["watching", "stopped", "degraded", "not_configured"].includes(settings.vault.sync_state) ||
    typeof settings.vault.configured !== "boolean" ||
    typeof settings.vault.watcher_running !== "boolean" ||
    (settings.vault.managed_directory !== null &&
      !safeRelativeDirectory(settings.vault.managed_directory)) ||
    !settings.providers ||
    !settings.retrieval ||
    !settings.chunking ||
    !settings.video ||
    !settings.maintenance
  ) {
    return false;
  }
  const providerKeys: Array<ProviderSettings["capability"]> = [
    "chat",
    "embedding",
    "asr",
    "vision",
    "reranker",
  ];
  const providerEntries = providerKeys.map((key) => settings.providers[key]);
  const providers = Object.values(settings.providers);
  if (
    Object.keys(settings.providers).sort().join(",") !== providerKeys.slice().sort().join(",") ||
    providers.length !== 5 ||
    providerEntries.some(
      (provider) =>
        !provider ||
        provider.capability !== providerKeys[providerEntries.indexOf(provider)] ||
        !["openai-compatible", "fastembed"].includes(provider.provider_kind) ||
        typeof provider.configured !== "boolean" ||
        typeof provider.credential_configured !== "boolean" ||
        (provider.model !== null && !safeModel(provider.model)),
    )
  ) {
    return false;
  }
  if (
    !finiteInteger(settings.retrieval.rag_query_max_chars, 1) ||
    !finiteInteger(settings.retrieval.rrf_k, 1) ||
    !finiteInteger(settings.retrieval.fts_limit, 1) ||
    !finiteInteger(settings.retrieval.vector_limit, 1) ||
    !finiteNumber(settings.retrieval.threshold) ||
    !finiteInteger(settings.retrieval.confident_rank, 1) ||
    !finiteInteger(settings.retrieval.rerank_limit, 1) ||
    settings.chunking.strategy !== "paragraph_then_fixed_width" ||
    !finiteInteger(settings.chunking.max_chars, 1) ||
    !["permanent", "until_expiry", "delete_after_processing"].includes(
      settings.video.retention_policy,
    ) ||
    !finiteInteger(settings.video.retention_days) ||
    !finiteInteger(settings.video.max_bytes, 1) ||
    !finiteInteger(settings.video.max_duration_seconds, 1) ||
    !["healthy", "degraded", "not_configured", "configured", "unavailable"].includes(
      settings.video.ffmpeg_state,
    ) ||
    typeof settings.maintenance.backup_available !== "boolean" ||
    typeof settings.maintenance.rescan_available !== "boolean" ||
    typeof settings.maintenance.rebuild_available !== "boolean" ||
    !safeText(settings.maintenance.configuration_hint) ||
    !safeText(settings.maintenance.restore_note)
  ) {
    return false;
  }
  return true;
}

const invalidMaintenanceResponse = "unsafe_maintenance_response";
const maintenanceResponseError = "维护响应无效，已隐藏不安全内容";

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message === invalidMaintenanceResponse) {
    return maintenanceResponseError;
  }
  return error instanceof ApiError ? error.message : fallback;
}

function ProviderCard({ provider }: { provider: ProviderSettings }) {
  const label = providerLabels[provider.capability];
  return (
    <article className="settings-provider-card">
      <div className="settings-card-heading">
        <div>
          <span className="eyebrow">Provider</span>
          <h3>{label}</h3>
        </div>
        <span className={"settings-state " + (provider.configured ? "is-ready" : "is-muted")}>
          {provider.configured ? "已配置" : "未配置"}
        </span>
      </div>
      <dl className="settings-definition-list">
        <div>
          <dt>类型</dt>
          <dd>{providerKinds[provider.provider_kind]}</dd>
        </div>
        <div>
          <dt>模型</dt>
          <dd>{provider.model ?? "未填写"}</dd>
        </div>
        <div>
          <dt>凭据</dt>
          <dd>{provider.credential_configured ? "已配置（不显示）" : "未配置"}</dd>
        </div>
      </dl>
      <p className="settings-card-note">配置状态不等于已验证；运行验证请查看首页实时探针。</p>
    </article>
  );
}

function SettingsValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="settings-value-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function SettingsPage() {
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"rescan" | "backup" | "rebuild" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getSettings();
      if (!isSafeSettings(next)) {
        throw new Error("unsafe_settings_response");
      }
      setSettings(next);
    } catch (requestError) {
      setSettings(null);
      setError(
        requestError instanceof Error && requestError.message === "unsafe_settings_response"
          ? "设置响应无效，已隐藏不安全内容"
          : errorMessage(requestError, "设置加载失败"),
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const runRescan = async () => {
    if (busy) return;
    setBusy("rescan");
    setError(null);
    setNotice(null);
    try {
      const result = await rescanSettings();
      if (!isSafeRescanResponse(result)) {
        throw new Error(invalidMaintenanceResponse);
      }
      setNotice("重新扫描完成：发现 " + (result.changed + result.renamed) + " 项变化");
      await refresh();
    } catch (requestError) {
      setError(errorMessage(requestError, "重新扫描失败"));
    } finally {
      setBusy(null);
    }
  };

  const runBackup = async () => {
    if (busy || !window.confirm("创建备份可能需要较长时间，确定继续吗？")) return;
    setBusy("backup");
    setError(null);
    setNotice(null);
    try {
      const result = await createBackup();
      if (!isSafeBackupResponse(result)) {
        throw new Error(invalidMaintenanceResponse);
      }
      setNotice("备份已创建（归档 ID：" + result.archive_id + "）");
    } catch (requestError) {
      setError(errorMessage(requestError, "备份创建失败"));
    } finally {
      setBusy(null);
    }
  };

  const runRebuild = async () => {
    if (
      busy ||
      !window.confirm("将重建 SQLite FTS5 与 Qdrant 派生索引，不会修改 Markdown。确定继续吗？")
    ) {
      return;
    }
    setBusy("rebuild");
    setError(null);
    setNotice(null);
    try {
      const result = await rebuildDerivedState();
      if (!isSafeRebuildResponse(result)) {
        throw new Error(invalidMaintenanceResponse);
      }
      setNotice(
        "派生索引已重建：" + result.published_items + " 个条目，" + result.chunks + " 个分块",
      );
    } catch (requestError) {
      setError(errorMessage(requestError, "派生索引重建失败"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="stage-page settings-page">
      <div className="stage-heading settings-heading">
        <div>
          <span className="eyebrow">本机设置</span>
          <h1>设置</h1>
          <p>这里只观察安全配置并执行受控维护；API Key 不会返回到前端。</p>
        </div>
        <button className="ghost-button" type="button" onClick={() => void refresh()} disabled={loading || Boolean(busy)}>
          {loading ? "加载中…" : "刷新设置"}
        </button>
      </div>

      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {notice ? <div className="inline-message" role="status">{notice}</div> : null}
      {loading && !settings ? <p className="quiet-note">正在读取本机设置…</p> : null}

      {settings ? (
        <div className="settings-grid">
          <article className="settings-card settings-wide-card">
            <div className="settings-card-heading">
              <div>
                <span className="eyebrow">Vault / Sync</span>
                <h2>Vault 与同步</h2>
              </div>
              <span className={"settings-state " + (settings.vault.configured ? "is-ready" : "is-muted")}>
                {settings.vault.configured ? "已配置" : "未配置"}
              </span>
            </div>
            <dl className="settings-definition-list settings-vault-list">
              <SettingsValue label="受管理目录" value={settings.vault.managed_directory ?? "未配置"} />
              <SettingsValue label="Watcher" value={settings.vault.watcher_running ? "运行中" : "未运行"} />
              <SettingsValue label="同步状态" value={settings.vault.sync_state} />
              <SettingsValue label="本机绑定" value={settings.bind_host} />
            </dl>
          </article>

          <section className="settings-section settings-wide-card">
            <div className="settings-section-heading">
              <div>
                <span className="eyebrow">Capabilities</span>
                <h2>模型能力</h2>
              </div>
              <span className="settings-section-note">配置与运行验证分开显示</span>
            </div>
            <div className="settings-provider-grid">
              {Object.values(settings.providers).map((provider) => (
                <ProviderCard key={provider.capability} provider={provider} />
              ))}
            </div>
          </section>

          <article className="settings-card">
            <span className="eyebrow">Retrieval</span>
            <h2>检索与切分</h2>
            <dl className="settings-definition-list settings-spaced-list">
              <SettingsValue label="查询上限" value={settings.retrieval.rag_query_max_chars + " 字符"} />
              <SettingsValue label="RRF k" value={String(settings.retrieval.rrf_k)} />
              <SettingsValue label="FTS 上限" value={String(settings.retrieval.fts_limit)} />
              <SettingsValue label="向量上限" value={String(settings.retrieval.vector_limit)} />
              <SettingsValue label="阈值" value={String(settings.retrieval.threshold)} />
              <SettingsValue label="置信排名" value={String(settings.retrieval.confident_rank)} />
              <SettingsValue label="重排上限" value={String(settings.retrieval.rerank_limit)} />
              <SettingsValue
                label="切分策略"
                value={settings.chunking.strategy + " · " + settings.chunking.max_chars + " 字符"}
              />
            </dl>
          </article>

          <article className="settings-card">
            <span className="eyebrow">Video</span>
            <h2>视频与 FFmpeg</h2>
            <dl className="settings-definition-list settings-spaced-list">
              <SettingsValue label="媒体保留" value={settings.video.retention_policy} />
              <SettingsValue label="保留天数" value={String(settings.video.retention_days)} />
              <SettingsValue label="大小上限" value={Math.round(settings.video.max_bytes / 1_000_000) + " MB"} />
              <SettingsValue
                label="时长上限"
                value={Math.round(settings.video.max_duration_seconds / 60) + " 分钟"}
              />
              <SettingsValue label="FFmpeg" value={stateLabels[settings.video.ffmpeg_state]} />
            </dl>
            {settings.video.ffmpeg_state === "not_configured" ? (
              <p className="settings-card-note">FFmpeg 未配置只影响视频能力；请稍后按人工步骤安装。</p>
            ) : null}
          </article>

          <article className="settings-card settings-wide-card settings-maintenance-card">
            <div className="settings-card-heading">
              <div>
                <span className="eyebrow">Maintenance</span>
                <h2>数据维护</h2>
              </div>
              <span className="settings-local-note">{settings.local_only ? "仅本机" : "非本机绑定"}</span>
            </div>
            <p className="settings-card-note">{settings.maintenance.configuration_hint}</p>
            <div className="settings-action-grid">
              <button
                className="ghost-button"
                type="button"
                onClick={() => void runRescan()}
                disabled={Boolean(busy) || !settings.maintenance.rescan_available}
              >
                {busy === "rescan" ? "扫描中…" : "重新扫描"}
              </button>
              <button
                className="ghost-button"
                type="button"
                onClick={() => void runBackup()}
                disabled={Boolean(busy) || !settings.maintenance.backup_available}
              >
                {busy === "backup" ? "备份中…" : "创建备份"}
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => void runRebuild()}
                disabled={Boolean(busy) || !settings.maintenance.rebuild_available}
              >
                {busy === "rebuild" ? "重建中…" : "重建派生索引"}
              </button>
            </div>
            <div className="settings-restore-note" role="note">
              <strong>恢复</strong>
              <span>{settings.maintenance.restore_note}</span>
            </div>
          </article>

          <article className="settings-card settings-wide-card settings-security-card">
            <span className="eyebrow">安全边界</span>
            <p>设置页不编辑 .env、不接收路径或密钥。修改项目根目录 .env 后重启服务生效。</p>
          </article>
        </div>
      ) : null}
    </section>
  );
}
