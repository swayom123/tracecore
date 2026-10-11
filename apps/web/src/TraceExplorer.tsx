import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import { fetchRuns, fetchTrace, type RunFilters } from "./api";
import EvaluationExplorer from "./EvaluationExplorer";
import ExperimentExplorer from "./ExperimentExplorer";
import AgentAnalyticsExplorer from "./AgentAnalyticsExplorer";
import GovernanceExplorer from "./GovernanceExplorer";
import MonitoringExplorer from "./MonitoringExplorer";
import type { Run, Span, TraceDetail } from "./types";

const EMPTY_FILTERS: RunFilters = {
  environment: "",
  application_id: "",
  agent_name: "",
  agent_version: "",
  status: "",
  started_after: "",
  started_before: "",
};

function TraceLogo({ compact = false }: { compact?: boolean }) {
  return <span className={`trace-logo ${compact ? "compact" : ""}`} aria-label="TraceCore">
    <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 2.8 28.2 9.4v13.2L16 29.2 3.8 22.6V9.4L16 2.8Z"/><path d="m16 2.8 12.2 6.6L16 16 3.8 9.4M16 16v13.2M9.7 12.8v6.4l6.3 3.4 6.3-3.4v-6.4"/></svg>
    {!compact && <span className="trace-logo-type"><b><span>Trace</span><i>Core</i></b><small>OBSERVABILITY</small></span>}
  </span>;
}

function fmtDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function fmtDuration(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (value < 1000) return `${value.toFixed(value < 10 ? 1 : 0)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function jsonText(value: unknown): string {
  if (value === undefined || value === null) return "Not recorded";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function buildChildren(spans: Span[]): Map<string | null, Span[]> {
  const children = new Map<string | null, Span[]>();
  for (const span of spans) {
    const key = span.parent_span_id;
    children.set(key, [...(children.get(key) ?? []), span]);
  }
  for (const items of children.values())
    items.sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
  return children;
}

function flattenTree(
  rootId: string,
  spans: Span[],
): Array<{ span: Span; depth: number }> {
  const byId = new Map(spans.map((span) => [span.id, span]));
  const children = buildChildren(spans);
  const rows: Array<{ span: Span; depth: number }> = [];
  const visit = (id: string, depth: number, ancestors: Set<string>) => {
    if (ancestors.has(id)) return;
    const span = byId.get(id);
    if (!span) return;
    rows.push({ span, depth });
    const next = new Set(ancestors).add(id);
    for (const child of children.get(id) ?? [])
      visit(child.id, depth + 1, next);
  };
  visit(rootId, 0, new Set());
  return rows;
}

function SpanDetails({ span, onClose }: { span: Span; onClose: () => void }) {
  return (
    <aside className="span-panel" aria-label="Span details">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">{span.span_type} SPAN</div>
          <h2>{span.name}</h2>
        </div>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="Close span details"
        >
          ×
        </button>
      </div>
      <dl className="detail-grid">
        <dt>Span ID</dt>
        <dd className="mono">{span.id}</dd>
        <dt>Parent</dt>
        <dd className="mono">{span.parent_span_id ?? "Root span"}</dd>
        <dt>Status</dt>
        <dd>
          <Status value={span.status} />
        </dd>
        <dt>Started</dt>
        <dd>{fmtDate(span.started_at)}</dd>
        <dt>Ended</dt>
        <dd>{fmtDate(span.ended_at)}</dd>
        <dt>Duration</dt>
        <dd>{fmtDuration(span.duration_ms)}</dd>
      </dl>
      {span.error && (
        <section className="detail-section">
          <h3>Error</h3>
          <pre className="error-content">{span.error}</pre>
        </section>
      )}
      <section className="detail-section">
        <h3>Attributes</h3>
        <pre>{jsonText(span.attributes)}</pre>
      </section>
      <section className="detail-section">
        <h3>Input metadata</h3>
        <pre>{jsonText(span.input)}</pre>
      </section>
      <section className="detail-section">
        <h3>Output metadata</h3>
        <pre>{jsonText(span.output)}</pre>
      </section>
    </aside>
  );
}

function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value.toLowerCase()}`}>{value}</span>
  );
}

function TraceView({
  traceId,
  onBack,
}: {
  traceId: string;
  onBack: () => void;
}) {
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [selected, setSelected] = useState<Span | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchTrace(traceId)
      .then((value) => {
        if (active) setTrace(value);
      })
      .catch((reason: unknown) => {
        if (active)
          setError(
            reason instanceof Error ? reason.message : "Unable to load trace",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [traceId]);

  const rows = useMemo(
    () => (trace ? flattenTree(trace.trace.root_span_id, trace.spans) : []),
    [trace],
  );
  const bounds = useMemo(() => {
    if (!trace?.spans.length) return { start: 0, end: 0 };
    const starts = trace.spans
      .map((span) => Date.parse(span.started_at))
      .filter(Number.isFinite);
    const ends = trace.spans
      .map((span) => (span.ended_at ? Date.parse(span.ended_at) : Number.NaN))
      .filter(Number.isFinite);
    return { start: Math.min(...starts), end: Math.max(...ends, ...starts) };
  }, [trace]);
  const total = Math.max(bounds.end - bounds.start, 1);

  if (loading)
    return (
      <main className="content">
        <button className="back-button" onClick={onBack}>
          ← Runs
        </button>
        <p className="muted">Loading trace…</p>
      </main>
    );
  if (error || !trace)
    return (
      <main className="content">
        <button className="back-button" onClick={onBack}>
          ← Runs
        </button>
        <div className="notice error-notice">
          {error || "Trace unavailable"}
        </div>
      </main>
    );
  const run = trace.run;
  return (
    <main className="content trace-content">
      <button className="back-button" onClick={onBack}>
        ← Runs
      </button>
      <div className="trace-title">
        <div>
          <div className="eyebrow">TRACE · {trace.trace.status}</div>
          <h1>{run?.agent_name || run?.workflow_name || "Run trace"}</h1>
          <div className="mono muted">{trace.trace.trace_id}</div>
        </div>
        <div className="trace-meta">
          <Status value={run?.status ?? trace.trace.status} />
          <span>{fmtDuration(trace.trace.duration_ms)}</span>
          <time>{fmtDate(trace.trace.started_at)}</time>
        </div>
      </div>
      {run && (
        <div className="run-context">
          <span>
            Run <code>{run.id}</code>
          </span>
          <span>
            App <code>{run.application_id}</code>
          </span>
          <span>
            Environment <code>{run.environment}</code>
          </span>
          <span>
            Agent version{" "}
            <code>
              {run.agent_version
                ? `${run.agent_version} (${run.agent_version_id})`
                : "Not recorded"}
            </code>
          </span>
        </div>
      )}
      <section className="trace-card">
        <div className="section-heading">
          <div>
            <h2>Execution timeline</h2>
            <p>
              Span timing is positioned from recorded timestamps. Select a row
              to inspect its details.
            </p>
          </div>
          <span className="span-count">{trace.spans.length} spans</span>
        </div>
        <div className="waterfall-header">
          <span>SPAN</span>
          <span>STATUS</span>
          <span>DURATION</span>
          <span className="timeline-axis">
            TIMELINE <small>relative to trace start</small>
          </span>
        </div>
        {rows.map(({ span, depth }) => {
          const start = Date.parse(span.started_at);
          const end = span.ended_at ? Date.parse(span.ended_at) : Number.NaN;
          const left = Number.isFinite(start)
            ? Math.max(0, ((start - bounds.start) / total) * 100)
            : 0;
          const width = Number.isFinite(end)
            ? Math.max(0.8, ((end - start) / total) * 100)
            : 0.8;
          return (
            <button
              className={`waterfall-row ${selected?.id === span.id ? "selected" : ""}`}
              key={span.id}
              onClick={() => setSelected(span)}
            >
              <span
                className="span-name-cell"
                style={{ paddingLeft: `${14 + depth * 20}px` }}
              >
                <i
                  className={`type-marker marker-${span.span_type.toLowerCase()}`}
                />
                <span className="span-name">
                  <b>{span.name}</b>
                  <small>
                    {span.span_type} · <code>{span.id.slice(0, 12)}</code>
                  </small>
                </span>
              </span>
              <span>
                <Status value={span.status} />
              </span>
              <span className="duration-cell">
                {fmtDuration(span.duration_ms)}
              </span>
              <span className="timeline-track">
                <span
                  className={`timeline-bar bar-${span.status.toLowerCase()}`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  title={`${fmtDate(span.started_at)} · ${fmtDuration(span.duration_ms)}`}
                />
              </span>
            </button>
          );
        })}
        {rows.length === 0 && (
          <div className="empty-inline">This trace has no spans.</div>
        )}
      </section>
      <section className="trace-card tree-card">
        <div className="section-heading">
          <div>
            <h2>Span hierarchy</h2>
            <p>Parent and child relationships recorded for this run.</p>
          </div>
        </div>
        <div className="tree-list">
          {rows.map(({ span, depth }) => (
            <button
              className="tree-row"
              key={span.id}
              style={{ paddingLeft: `${14 + depth * 22}px` }}
              onClick={() => setSelected(span)}
            >
              <span className="tree-branch">{depth ? "↳" : "●"}</span>
              <span>{span.name}</span>
              <span className="tree-type">{span.span_type}</span>
              <Status value={span.status} />
            </button>
          ))}
        </div>
      </section>
      {selected && (
        <SpanDetails span={selected} onClose={() => setSelected(null)} />
      )}
    </main>
  );
}

export default function TraceExplorer() {
  const [apiToken, setApiToken] = useState(() => window.sessionStorage.getItem("tracecore_api_token") ?? "");
  const [filters, setFilters] = useState<RunFilters>(EMPTY_FILTERS);
  const [draft, setDraft] = useState<RunFilters>(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [pageData, setPageData] = useState<{
    runs: Run[];
    total: number;
    sample: Run[];
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(true);
  const [traceId, setTraceId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("trace"),
  );
  const [activeView, setActiveView] = useState<"runs" | "evaluations" | "experiments" | "agent-analytics" | "governance" | "monitoring">("runs");
  const pageSize = 25;

  const load = useCallback(() => {
    let active = true;
    setLoading(true);
    setError("");
    fetchRuns(filters, page * pageSize, pageSize)
      .then((result) => {
        if (active) setPageData({ runs: result.runs.slice(0, pageSize), total: result.total, sample: result.runs });
      })
      .catch((reason: unknown) => {
        if (active)
          setError(
            reason instanceof Error ? reason.message : "Unable to load runs",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [filters, page]);
  useEffect(() => load(), [load]);
  useEffect(() => {
    const update = () =>
      setTraceId(new URLSearchParams(window.location.search).get("trace"));
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  const openTrace = (run: Run) => {
    window.history.pushState(
      {},
      "",
      `?trace=${encodeURIComponent(run.trace_id)}`,
    );
    setTraceId(run.trace_id);
  };
  const backToRuns = () => {
    window.history.pushState({}, "", window.location.pathname);
    setTraceId(null);
  };
  const applyFilters = (event: FormEvent) => {
    event.preventDefault();
    setPage(0);
    setFilters({ ...draft });
  };
  const clearFilters = () => {
    setDraft(EMPTY_FILTERS);
    setPage(0);
    setFilters(EMPTY_FILTERS);
  };

  return (
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <button className="collapse-sidebar" onClick={() => setSidebarCollapsed(!sidebarCollapsed)} aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}>{sidebarCollapsed ? "›" : "‹"}</button>
        <div className="brand">
          <TraceLogo />
        </div>
        <nav>
          <div className="nav-label">MONITOR</div>
          <button
            className={`nav-item ${activeView === "runs" ? "active" : ""}`}
            onClick={() => {
              setActiveView("runs");
              setTraceId(null);
            }}
          >
            <span className="nav-icon">▤</span><b>Runs</b>
          </button>
          <button
            className={`nav-item ${activeView === "evaluations" ? "active" : ""}`}
            onClick={() => {
              setActiveView("evaluations");
              setTraceId(null);
            }}
          >
            <span className="nav-icon">◷</span><b>Evaluations</b>
          </button>
          <button
            className={`nav-item ${activeView === "experiments" ? "active" : ""}`}
            onClick={() => {
              setActiveView("experiments");
              setTraceId(null);
            }}
          >
            <span className="nav-icon">⇄</span><b>Experiments</b>
          </button>
          <button
            className={`nav-item ${activeView === "agent-analytics" ? "active" : ""}`}
            onClick={() => {
              setActiveView("agent-analytics");
              setTraceId(null);
            }}
          >
            <span className="nav-icon">⌁</span><b>Agent Analytics</b>
          </button>
          <button
            className={`nav-item ${activeView === "governance" ? "active" : ""}`}
            onClick={() => {
              setActiveView("governance");
              setTraceId(null);
            }}
          >
            <span className="nav-icon">◇</span><b>Governance</b>
          </button>
          <button
            className={`nav-item ${activeView === "monitoring" ? "active" : ""}`}
            onClick={() => {
              setActiveView("monitoring");
              setTraceId(null);
            }}
          >
            <span className="nav-icon">⌁</span><b>Monitoring</b>
          </button>
        </nav>
        <div className="sidebar-bottom">
          <label className="token-entry">
            API bearer token
            <input
              aria-label="API bearer token"
              type="password"
              autoComplete="off"
              value={apiToken}
              onChange={(event) => {
                setApiToken(event.target.value);
                if (event.target.value) window.sessionStorage.setItem("tracecore_api_token", event.target.value);
                else window.sessionStorage.removeItem("tracecore_api_token");
              }}
              placeholder="Local development may be blank"
            />
          </label>
            <a href="/docs" target="_blank" rel="noreferrer">Developer console ↗</a>
        </div>
      </aside>
      {traceId ? (
        <TraceView traceId={traceId} onBack={backToRuns} />
      ) : activeView === "evaluations" ? (
        <EvaluationExplorer />
      ) : activeView === "experiments" ? (
        <ExperimentExplorer />
      ) : activeView === "agent-analytics" ? (
        <AgentAnalyticsExplorer />
      ) : activeView === "governance" ? (
        <GovernanceExplorer />
      ) : activeView === "monitoring" ? (
        <MonitoringExplorer />
      ) : (
        <main className="content">
          <div className="topbar"><button className="mobile-menu" onClick={() => setSidebarCollapsed(!sidebarCollapsed)} aria-label="Toggle navigation">☰</button><span>Workspace <b>/</b> Runs</span><span className="topbar-context">Trace data · API connected</span></div>
          <header className="page-header">
            <div>
              <div className="eyebrow">OBSERVABILITY / RUNS</div>
              <h1>Runs</h1>
              <p>Inspect agent and workflow executions from ingested traces.</p>
            </div>
            <button className="refresh-button" onClick={() => load()}>
              ↻ <span>Refresh</span>
            </button>
          </header>
          <section className={`filter-panel ${filtersOpen ? "" : "is-collapsed"}`}>
          <button className="filter-toggle" type="button" aria-expanded={filtersOpen} onClick={() => setFiltersOpen(!filtersOpen)}><span><b>Filters</b><small>Refine runs by environment, agent, status, or time</small></span><span>{filtersOpen ? "⌃" : "⌄"}</span></button>
          {filtersOpen && <form className="filters" onSubmit={applyFilters}>
            <label>
              Environment
              <input
                value={draft.environment}
                onChange={(e) =>
                  setDraft({ ...draft, environment: e.target.value })
                }
                placeholder="e.g. production"
              />
            </label>
            <label>
              Application ID
              <input
                value={draft.application_id}
                onChange={(e) =>
                  setDraft({ ...draft, application_id: e.target.value })
                }
                placeholder="Application"
              />
            </label>
            <label>
              Agent
              <input
                value={draft.agent_name}
                onChange={(e) =>
                  setDraft({ ...draft, agent_name: e.target.value })
                }
                placeholder="Agent name"
              />
            </label>
            <label>
              Agent version
              <input
                value={draft.agent_version}
                onChange={(e) =>
                  setDraft({ ...draft, agent_version: e.target.value })
                }
                placeholder="Exact version"
              />
            </label>
            <label>
              Status
              <select
                value={draft.status}
                onChange={(e) => setDraft({ ...draft, status: e.target.value })}
              >
                <option value="">All statuses</option>
                {[
                  "SUCCEEDED",
                  "FAILED",
                  "PARTIAL",
                  "RUNNING",
                  "QUEUED",
                  "CANCELLED",
                ].map((status) => (
                  <option key={status}>{status}</option>
                ))}
              </select>
            </label>
            <label>
              Started after
              <input
                type="datetime-local"
                value={draft.started_after}
                onChange={(e) =>
                  setDraft({ ...draft, started_after: e.target.value })
                }
              />
            </label>
            <label>
              Started before
              <input
                type="datetime-local"
                value={draft.started_before}
                onChange={(e) =>
                  setDraft({ ...draft, started_before: e.target.value })
                }
              />
            </label>
            <div className="filter-actions">
              <button
                type="button"
                className="text-button"
                onClick={clearFilters}
              >
                Clear all
              </button>
              <button type="submit" className="primary-button">
                Apply filters
              </button>
            </div>
          </form>}
          </section>
          <section className="kpi-grid" aria-label="Run metrics">
            {(() => { const sample = pageData?.sample ?? []; const success = sample.filter((run) => ["SUCCEEDED", "SUCCESS", "COMPLETED"].includes(run.status.toUpperCase())).length; const failed = sample.filter((run) => ["FAILED", "ERROR"].includes(run.status.toUpperCase())).length; const durations = sample.map((run) => run.duration_ms).filter((value): value is number => value !== null && Number.isFinite(value)); const avg = durations.length ? durations.reduce((sum, value) => sum + value, 0) / durations.length : null; return [
              ["Total runs", pageData ? pageData.total.toLocaleString() : "—", "⌁", "blue", "Matching records"],
              ["Successful runs", pageData ? success.toLocaleString() : "—", "✓", "green", "Current page · up to 25 runs"],
              ["Failed runs", pageData ? failed.toLocaleString() : "—", "!", "red", "Current page · up to 25 runs"],
              ["Avg. execution time", avg === null ? "Unavailable" : fmtDuration(avg), "◷", "violet", durations.length ? `From ${durations.length} completed durations` : "No duration data"],
            ].map(([label, value, icon, tone, note]) => <article className={`kpi-card tone-${tone}`} key={label}><div className="kpi-icon">{icon}</div><span className="kpi-label">{label}</span><strong>{value}</strong><small>{note}</small></article>); })()}
          </section>
          <section className="runs-card">
            <div className="table-heading">
              <div>
                <h2>Recent runs</h2>
                <p>
                  {pageData
                    ? `${pageData.total.toLocaleString()} matching runs`
                    : "Run records from your applications"}
                </p>
              </div>
              <span className="live-label">INGESTED DATA</span>
            </div>
            {error && <div role="alert" className="api-error-state"><span className="empty-icon">!</span><div><h3>{/401|403|unauthor/i.test(error) ? "Authentication required" : /404|not found/i.test(error) ? "Runs endpoint unavailable" : /network|fetch|failed to fetch/i.test(error) ? "Backend is unreachable" : "Could not load runs"}</h3><p>{error}</p></div><button className="primary-button" onClick={() => load()}>Retry</button></div>}
            {loading && !pageData ? (
              <div className="loading-state">Loading runs…</div>
            ) : null}
            {!loading && !error && pageData?.total === 0 && (
              <div className="empty-state">
                <div className="empty-icon">⌁</div>
                <h3>{Object.values(filters).some(Boolean) ? "No runs match these filters." : "No runs have been ingested."}</h3>
                <p>
                  {Object.values(filters).some(Boolean) ? "Try adjusting or clearing your filters to see more runs." : "Instrument an application with the TraceCore SDK and send a run to this project. Ingested runs will appear here."}
                </p>
                <a href="/docs" target="_blank" rel="noreferrer">
                  Open API documentation ↗
                </a>
              </div>
            )}
            {pageData && pageData.total > 0 && (
              <>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>TIME</th>
                        <th>APPLICATION</th>
                        <th>AGENT / WORKFLOW</th>
                        <th>VERSION</th>
                        <th>ENVIRONMENT</th>
                        <th>STATUS</th>
                        <th>DURATION</th>
                        <th>RUN ID</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageData.runs.map((run) => (
                        <tr
                          key={run.id}
                          onClick={() => openTrace(run)}
                          tabIndex={0}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") openTrace(run);
                          }}
                        >
                          <td className="time-cell">
                            {fmtDate(run.started_at)}
                          </td>
                          <td>{run.application_id}</td>
                          <td>{run.agent_name || run.workflow_name || "—"}</td>
                          <td className="mono version-cell">
                            {run.agent_version || "—"}
                          </td>
                          <td>{run.environment}</td>
                          <td>
                            <Status value={run.status} />
                          </td>
                          <td>{fmtDuration(run.duration_ms)}</td>
                          <td className="mono id-cell">{run.id}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              <div className="pagination">
                  <span>
                    Showing {page * pageSize + 1}–
                    {Math.min((page + 1) * pageSize, pageData.total)} of{" "}
                    {pageData.total}
                  </span>
                  <div>
                    <button
                      disabled={page === 0 || loading}
                      onClick={() => setPage((value) => value - 1)}
                    >
                      Previous
                    </button>
                    <button
                      disabled={
                        (page + 1) * pageSize >= pageData.total || loading
                      }
                      onClick={() => setPage((value) => value + 1)}
                    >
                      Next
                    </button>
                  </div>
                </div>
              </>
            )}
          </section>
          <footer className="page-footer">
            TraceCore displays the data applications send. Missing values
            indicate fields were not recorded.
          </footer>
        </main>
      )}
    </div>
  );
}
