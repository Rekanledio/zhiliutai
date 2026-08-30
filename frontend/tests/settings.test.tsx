import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { SettingsPage } from "../src/features/settings/SettingsPage";

const settingsResponse = {
  local_only: true,
  bind_host: "127.0.0.1",
  vault: {
    configured: true,
    managed_directory: "知流台",
    watcher_running: true,
    sync_state: "watching",
  },
  providers: {
    chat: {
      capability: "chat",
      provider_kind: "openai-compatible",
      configured: true,
      credential_configured: true,
      model: "synthetic-chat",
    },
    embedding: {
      capability: "embedding",
      provider_kind: "fastembed",
      configured: true,
      credential_configured: false,
      model: "synthetic-embedding",
    },
    asr: {
      capability: "asr",
      provider_kind: "faster-whisper",
      configured: true,
      credential_configured: false,
      model: "medium",
    },
    vision: {
      capability: "vision",
      provider_kind: "openai-compatible",
      configured: false,
      credential_configured: false,
      model: null,
    },
    reranker: {
      capability: "reranker",
      provider_kind: "sentence-transformers",
      configured: true,
      credential_configured: false,
      model: "BAAI/bge-reranker-v2-m3",
    },
  },
  retrieval: {
    rag_query_max_chars: 2000,
    rrf_k: 60,
    fts_limit: 30,
    vector_limit: 30,
    threshold: 0.35,
    confident_rank: 3,
    rerank_limit: 20,
  },
  chunking: { strategy: "paragraph_then_fixed_width", max_chars: 800 },
  video: {
    retention_policy: "delete_after_processing",
    retention_days: 7,
    max_bytes: 500000000,
    max_duration_seconds: 14400,
    ffmpeg_state: "not_configured",
  },
  maintenance: {
    backup_available: true,
    rescan_available: true,
    rebuild_available: true,
    configuration_hint: "配置通过项目根目录 .env，重启后生效；API Key 仅在后端秘密配置中使用。",
    restore_note: "恢复必须先停止服务，再按文档化离线 CLI 执行；设置页不提供在线恢复。",
  },
};

function responseWith(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "X-Request-ID": "settings-test" }),
    json: async () => body,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("设置页面", () => {
  test("展示五项能力、脱敏边界和三个受控维护操作", async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/settings" && (init.method ?? "GET") === "GET") {
        return responseWith(settingsResponse);
      }
      if (url === "/api/settings/rescan" && init.method === "POST") {
        return responseWith({
          changed: 1,
          renamed: 0,
          missing: 0,
          conflicts: 0,
          invalid: 0,
          deferred: 0,
        });
      }
      if (url === "/api/settings/backup" && init.method === "POST") {
        return responseWith(
          {
            archive_id: "backup-" + "a".repeat(32),
            created_at: "2026-08-29T00:00:00Z",
            sha256: "b".repeat(64),
            config_key: "BACKUP_ROOT",
          },
          201,
        );
      }
      if (url === "/api/settings/rebuild" && init.method === "POST") {
        return responseWith({ published_items: 2, chunks: 3 });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<SettingsPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "设置" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "模型能力" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Chat" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Embedding" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "ASR" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Vision" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Reranker" })).toBeTruthy();
    expect(screen.getByText("Faster Whisper")).toBeTruthy();
    expect(screen.getByText("Sentence Transformers")).toBeTruthy();
    expect(screen.getByText("FFmpeg 未配置只影响视频能力；请稍后按人工步骤安装。")).toBeTruthy();
    expect(
      screen.getAllByText("配置状态不等于已验证；运行验证请查看首页实时探针。"),
    ).toHaveLength(5);

    fireEvent.click(screen.getByRole("button", { name: "重新扫描" }));
    expect((await screen.findByRole("status")).textContent).toContain("重新扫描完成");
    fireEvent.click(screen.getByRole("button", { name: "创建备份" }));
    expect((await screen.findByRole("status")).textContent).toContain("备份已创建");
    fireEvent.click(screen.getByRole("button", { name: "重建派生索引" }));
    expect((await screen.findByRole("status")).textContent).toContain("派生索引已重建");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/backup",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/rebuild",
      expect.objectContaining({ method: "POST" }),
    );
  });

  test("后端设置响应含绝对路径或凭据时整体 fail closed", async () => {
    const unsafe = {
      ...settingsResponse,
      providers: {
        ...settingsResponse.providers,
        chat: {
          ...settingsResponse.providers.chat,
          model: "C:\\Secrets\\COOKIE_SENTINEL",
        },
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => responseWith(unsafe)));
    render(<SettingsPage />);

    expect((await screen.findByRole("alert")).textContent).toContain(
      "设置响应无效，已隐藏不安全内容",
    );
    expect(screen.queryByText("C:\\Secrets\\COOKIE_SENTINEL")).toBeNull();
  });

  test("后端设置响应含任意 POSIX 绝对路径时整体 fail closed", async () => {
    const unsafe = {
      ...settingsResponse,
      maintenance: {
        ...settingsResponse.maintenance,
        configuration_hint: "/etc/POSIX_SETTINGS_SENTINEL",
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => responseWith(unsafe)));
    render(<SettingsPage />);

    expect((await screen.findByRole("alert")).textContent).toContain(
      "设置响应无效，已隐藏不安全内容",
    );
    expect(screen.queryByText("/etc/POSIX_SETTINGS_SENTINEL")).toBeNull();
  });

  test("维护操作响应不符合契约时不显示原值", async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/settings" && (init.method ?? "GET") === "GET") {
        return responseWith(settingsResponse);
      }
      if (url === "/api/settings/rescan" && init.method === "POST") {
        return responseWith({
          changed: "/etc/RESCAN_COUNT_SENTINEL",
          renamed: 0,
          missing: 0,
          conflicts: 0,
          invalid: 0,
          deferred: 0,
        });
      }
      if (url === "/api/settings/backup" && init.method === "POST") {
        return responseWith(
          {
            archive_id: "backup-Cookie_SECRET_SENTINEL",
            created_at: "2026-08-29T00:00:00Z",
            sha256: "b".repeat(64),
            config_key: "BACKUP_ROOT",
          },
          201,
        );
      }
      if (url === "/api/settings/rebuild" && init.method === "POST") {
        return responseWith({
          published_items: "REBUILD_COUNT_SENTINEL",
          chunks: 3,
        });
      }
      throw new Error("unhandled route " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<SettingsPage />);

    await screen.findByRole("heading", { level: 1, name: "设置" });
    fireEvent.click(screen.getByRole("button", { name: "重新扫描" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "维护响应无效，已隐藏不安全内容",
    );
    expect(screen.queryByText("/etc/RESCAN_COUNT_SENTINEL")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "创建备份" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "维护响应无效，已隐藏不安全内容",
    );
    expect(screen.queryByText("Cookie_SECRET_SENTINEL")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重建派生索引" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "维护响应无效，已隐藏不安全内容",
    );
    expect(screen.queryByText("REBUILD_COUNT_SENTINEL")).toBeNull();
  });
});
