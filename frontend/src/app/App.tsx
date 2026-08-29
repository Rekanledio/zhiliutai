import { useCallback, useEffect, useMemo, useState } from "react";

import {
  type DashboardResponse,
  type HealthComponent,
  type HealthState,
  getDashboard,
} from "../services/api";
import { InboxPage } from "../features/inbox/InboxPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { KnowledgePage } from "../features/knowledge/KnowledgePage";
import { SearchPage } from "../features/search/SearchPage";
import "../styles/global.css";

type PageKey =
  | "overview"
  | "inbox"
  | "library"
  | "collections"
  | "search"
  | "jobs"
  | "settings";

interface NavItem {
  key: PageKey;
  label: string;
  icon: string;
  section: string;
}

const navItems: NavItem[] = [
  { key: "overview", label: "今日总览", icon: "⌂", section: "工作台" },
  { key: "inbox", label: "收件箱", icon: "＋", section: "知识空间" },
  { key: "library", label: "知识库", icon: "▤", section: "知识空间" },
  { key: "collections", label: "合集", icon: "◇", section: "知识空间" },
  { key: "search", label: "搜索与问答", icon: "⌕", section: "检索" },
  { key: "jobs", label: "处理任务", icon: "◌", section: "系统" },
  { key: "settings", label: "设置", icon: "⚙", section: "系统" },
];

const stateLabel: Record<HealthState, string> = {
  healthy: "正常",
  degraded: "降级",
  not_configured: "未配置",
  configured: "已配置",
  unavailable: "不可用",
};

const stateTone: Record<HealthState, string> = {
  healthy: "is-healthy",
  degraded: "is-degraded",
  not_configured: "is-muted",
  configured: "is-configured",
  unavailable: "is-unavailable",
};

const fallbackHealth: DashboardResponse["health"] = {
  status: "degraded",
  checked_at: new Date().toISOString(),
  components: [
    { key: "api", label: "FastAPI", state: "degraded", detail: "尚未连接到本机 API" },
    { key: "sqlite", label: "SQLite", state: "degraded", detail: "API 未连接" },
    { key: "qdrant", label: "Qdrant Local", state: "degraded", detail: "API 未连接" },
    { key: "artifact_storage", label: "Artifact Storage", state: "degraded", detail: "API 未连接" },
    { key: "obsidian", label: "Obsidian Vault", state: "not_configured", detail: "尚未配置 Vault" },
    { key: "obsidian_watcher", label: "Obsidian Watcher", state: "not_configured", detail: "尚未配置 Vault" },
    { key: "model_providers", label: "Model Providers", state: "not_configured", detail: "尚未配置模型" },
    { key: "ffmpeg", label: "FFmpeg", state: "not_configured", detail: "仅影响后续视频能力" },
  ],
};

function fallbackDashboard(): DashboardResponse {
  const now = new Date();
  const weekday = new Intl.DateTimeFormat("zh-CN", { weekday: "long" }).format(now);
  return {
    greeting: now.getHours() < 12 ? "早上好" : now.getHours() < 18 ? "下午好" : "晚上好",
    date_label: (now.getMonth() + 1) + " 月 " + now.getDate() + " 日 · " + weekday,
    stats: { knowledge_count: 0, today_added: 0, pending_review: 0, processing: 0 },
    health: fallbackHealth,
    pending_reviews: [],
    recent_items: [],
    processing_jobs: [],
  };
}

function StatusDot({ state }: { state: HealthState }) {
  return <span className={"status-dot " + stateTone[state]} aria-hidden="true" />;
}

function StatusBadge({ state, detail }: { state: HealthState; detail?: string }) {
  const accessibleLabel = detail ? stateLabel[state] + "：" + detail : stateLabel[state];
  return (
    <span
      className={"status-badge " + stateTone[state]}
      aria-label={accessibleLabel}
      title={detail}
    >
      {stateLabel[state]}
    </span>
  );
}

function ServiceStatus({ components }: { components: HealthComponent[] }) {
  return (
    <div className="service-status">
      <div className="service-status-heading">
        <span>本机服务</span>
        <span className="service-status-caption">实时探针</span>
      </div>
      <div className="service-status-list">
        {components.map((component) => (
          <div className="service-row" key={component.key}>
            <div className="service-row-label">
              <StatusDot state={component.state} />
              <span>{component.label}</span>
            </div>
            <StatusBadge state={component.state} detail={component.detail} />
          </div>
        ))}
      </div>
    </div>
  );
}

function QuickCapture({
  icon,
  title,
  detail,
  tone,
  onClick,
}: {
  icon: string;
  title: string;
  detail: string;
  tone: string;
  onClick: () => void;
}) {
  return (
    <button className={"capture-card " + tone} onClick={onClick} type="button">
      <span className="capture-card-icon">{icon}</span>
      <span className="capture-card-copy">
        <strong>{title}</strong>
        <small>{detail}</small>
      </span>
      <span className="capture-card-arrow" aria-hidden="true">↗</span>
    </button>
  );
}

function StatCard({
  label,
  value,
  detail,
  icon,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  icon: string;
  tone: string;
}) {
  return (
    <article className={"stat-card " + tone}>
      <span className="stat-icon">{icon}</span>
      <div>
        <span className="eyebrow">{label}</span>
        <strong className="stat-value">{value}</strong>
        <span className="stat-detail">{detail}</span>
      </div>
    </article>
  );
}

function EmptyPanel({ icon, title, detail }: { icon: string; title: string; detail: string }) {
  return (
    <div className="empty-panel">
      <span className="empty-panel-icon">{icon}</span>
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

function Overview({
  dashboard,
  onCapture,
}: {
  dashboard: DashboardResponse;
  onCapture: (label: string) => void;
}) {
  const stats = dashboard.stats;
  const serviceHealth = useMemo(() => dashboard.health.components, [dashboard.health.components]);

  return (
    <div className="overview-page">
      <section className="welcome-row">
        <div>
          <span className="eyebrow">{dashboard.date_label}</span>
          <h1>{dashboard.greeting}，先整理一点点</h1>
          <p className="welcome-copy">把今天遇到的内容放进来，知流台会帮你留住脉络。</p>
        </div>
        <div className="local-badge">
          <span className="local-badge-pulse" />
          <span>仅在本机运行</span>
        </div>
      </section>

      <section className="capture-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">快速采集</span>
            <h2>先放进收件箱</h2>
          </div>
          <span className="section-note">确认后才会进入知识库</span>
        </div>
        <div className="capture-grid">
          <QuickCapture icon="✦" title="粘贴文本" detail="灵感、摘录或一段想法" tone="tone-peach" onClick={() => onCapture("粘贴文本")} />
          <QuickCapture icon="▧" title="上传文件" detail="Markdown、PDF 或 DOCX" tone="tone-lilac" onClick={() => onCapture("上传文件")} />
          <QuickCapture icon="⌁" title="添加网页" detail="保存一个静态网页来源" tone="tone-sage" onClick={() => onCapture("添加网页")} />
          <QuickCapture icon="▶" title="添加视频" detail="字幕优先，之后再处理画面" tone="tone-sand" onClick={() => onCapture("添加视频")} />
        </div>
      </section>

      <section className="stats-grid">
        <StatCard label="知识条目" value={String(stats.knowledge_count)} detail="确认后会出现在这里" icon="▤" tone="stat-teal" />
        <StatCard label="今日新增" value={String(stats.today_added)} detail="今天还没有新内容" icon="＋" tone="stat-peach" />
        <StatCard label="待确认" value={String(stats.pending_review)} detail="AI 结果不会自动发布" icon="◇" tone="stat-lilac" />
        <StatCard label="处理中" value={String(stats.processing)} detail="任务状态实时可见" icon="◌" tone="stat-sage" />
      </section>

      <section className="content-grid">
        <article className="content-card review-card">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">人工确认</span>
              <h2>需要你确认</h2>
            </div>
            <button className="text-button" type="button" onClick={() => onCapture("审核队列")}>查看队列 <span>↗</span></button>
          </div>
          {dashboard.pending_reviews.length === 0 ? (
            <EmptyPanel icon="✓" title="收件箱很安静" detail="新资料会先停在这里，等你确认摘要、标签和归属。" />
          ) : (
            <div className="item-list">
              {dashboard.pending_reviews.map((item, index) => (
                <div className="list-item" key={item.id ?? String(index)}>
                  <span className="list-item-marker" />
                  <div>
                    <strong>{item.title ?? "未命名资料"}</strong>
                    <span>{item.source_type ?? "来源待识别"}</span>
                  </div>
                  <button className="ghost-button" type="button">确认</button>
                </div>
              ))}
            </div>
          )}
        </article>

        <article className="content-card service-card">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">运行状态</span>
              <h2>本机健康度</h2>
            </div>
            <StatusBadge state={dashboard.health.status === "healthy" ? "healthy" : "degraded"} />
          </div>
          <ServiceStatus components={serviceHealth} />
          <p className="service-footnote">状态来自后端探针；未配置的服务会明确显示，不会伪装成正常。</p>
        </article>

        <article className="content-card recent-card">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">知识脉络</span>
              <h2>最近知识</h2>
            </div>
            <button className="text-button" type="button">进入知识库 <span>↗</span></button>
          </div>
          <EmptyPanel icon="⌁" title="你的知识会在这里展开" detail="确认后的 Markdown 会保留在 Obsidian，同时出现在知流台的索引里。" />
        </article>

        <article className="content-card jobs-card">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">异步处理</span>
              <h2>处理中</h2>
            </div>
            <button className="text-button" type="button">查看任务 <span>↗</span></button>
          </div>
          <EmptyPanel icon="◌" title="当前没有后台任务" detail="转录、解析和索引任务会在这里显示阶段和进度。" />
        </article>
      </section>
    </div>
  );
}

function PlaceholderPage({ item }: { item: NavItem }) {
  return (
    <section className="placeholder-page">
      <span className="placeholder-icon">{item.icon}</span>
      <span className="eyebrow">{item.section}</span>
      <h1>{item.label}</h1>
      <p>页面入口已经保留，相关功能正在本轮补齐。</p>
      <div className="placeholder-boundary">
        <span>当前状态</span>
        <strong>功能正在补齐</strong>
      </div>
    </section>
  );
}

export default function App() {
  const [activePage, setActivePage] = useState<PageKey>("overview");
  const [dashboard, setDashboard] = useState<DashboardResponse>(() => fallbackDashboard());
  const [loading, setLoading] = useState(true);
  const [apiError, setApiError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const nextDashboard = await getDashboard();
      setDashboard(nextDashboard);
      setApiError(null);
    } catch {
      setApiError("后端暂不可用，当前显示本机离线骨架");
      setDashboard(fallbackDashboard());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!toast) {
      return;
    }
    const timer = window.setTimeout(() => setToast(null), 2800);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const activeItem = navItems.find((item) => item.key === activePage) ?? navItems[0];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">知</div>
          <div>
            <strong>知流台</strong>
            <span>让知识自然流动</span>
          </div>
        </div>

        <nav className="primary-nav" aria-label="主导航">
          {["工作台", "知识空间", "检索", "系统"].map((section) => (
            <div className="nav-section" key={section}>
              <span className="nav-section-label">{section}</span>
              {navItems.filter((item) => item.section === section).map((item) => (
                <button
                  className={"nav-item " + (item.key === activePage ? "is-active" : "")}
                  key={item.key}
                  onClick={() => setActivePage(item.key)}
                  type="button"
                >
                  <span className="nav-item-icon" aria-hidden="true">{item.icon}</span>
                  <span>{item.label}</span>
                  {item.key === "inbox" && dashboard.stats.pending_review > 0 ? (
                    <span className="nav-count">{dashboard.stats.pending_review}</span>
                  ) : null}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <ServiceStatus components={dashboard.health.components} />
          <div className="sidebar-footer-note">
            <span>个人空间</span>
            <span>数据默认留在本机</span>
          </div>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <span>知流台</span>
            <span className="breadcrumb-divider">/</span>
            <strong>{activeItem.label}</strong>
          </div>
          <div className="topbar-actions">
            {apiError ? <span className="connection-note">{apiError}</span> : null}
            <button className="refresh-button" type="button" onClick={() => void refresh()} disabled={loading}>
              <span className={loading ? "refresh-icon is-spinning" : "refresh-icon"}>↻</span>
              {loading ? "同步中" : "刷新状态"}
            </button>
          </div>
        </header>

        {activePage === "overview" ? (
          <Overview
            dashboard={dashboard}
            onCapture={(label) => {
              if (label === "粘贴文本") {
                setActivePage("inbox");
              } else {
                setToast(label);
              }
            }}
          />
        ) : activePage === "inbox" ? (
          <InboxPage onChanged={() => void refresh()} />
        ) : activePage === "library" ? (
          <KnowledgePage />
        ) : activePage === "jobs" ? (
          <JobsPage />
        ) : activePage === "search" ? (
          <SearchPage />
        ) : (
          <PlaceholderPage item={activeItem} />
        )}
      </main>

      {toast ? (
        <div className="toast" role="status">
          <span className="toast-check">✓</span>
          <div>
            <strong>{toast}入口已准备</strong>
            <span>具体采集流程将在后续阶段接入</span>
          </div>
          <button type="button" aria-label="关闭提示" onClick={() => setToast(null)}>×</button>
        </div>
      ) : null}
    </div>
  );
}
