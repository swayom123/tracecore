import { useState } from "react";
import { apiFetch } from "./http";

type Metric = { value: number | null; status: string; sample_count: number };
type BudgetUsage = {
  value: number | null;
  limit: number;
  exceeded: boolean | null;
  exceeded_runs?: number;
  status: string;
};
type MonitorData = {
  project_id: string;
  run_count: number;
  metrics: Record<string, Metric | Record<string, unknown>>;
  budgets: Array<{ id: string; name: string; usage: Record<string, BudgetUsage> }>;
};
type AlertRule = {
  id: string;
  name: string;
  metric: string;
  operator: string;
  threshold: number;
  window_minutes: number;
  minimum_samples: number;
  severity: string;
  destination: { type: string };
};
type AlertOccurrence = {
  id: string;
  status: string;
  suppressed: boolean;
  fingerprint: string;
  evidence: Record<string, unknown>;
  deliveries: Array<{ status: string; destination_type: string; response_code: number | null }>;
};

export default function MonitoringExplorer() {
  const [project, setProject] = useState("");
  const [data, setData] = useState<MonitorData | null>(null);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [alerts, setAlerts] = useState<AlertOccurrence[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function load() {
    if (!project.trim()) {
      setError("Enter a project ID to inspect monitoring data.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const encoded = encodeURIComponent(project);
      const [metricResponse, ruleResponse, alertResponse] = await Promise.all([
        apiFetch(`/monitoring/metrics?project_id=${encoded}`),
        apiFetch(`/monitoring/alert-rules?project_id=${encoded}`),
        apiFetch(`/monitoring/alerts?project_id=${encoded}`),
      ]);
      for (const response of [metricResponse, ruleResponse, alertResponse]) {
        if (!response.ok) throw new Error(`Monitoring request failed (${response.status})`);
      }
      setData((await metricResponse.json()) as MonitorData);
      setRules(((await ruleResponse.json()) as { rules: AlertRule[] }).rules);
      setAlerts(
        ((await alertResponse.json()) as { occurrences: AlertOccurrence[] })
          .occurrences,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Monitoring data unavailable");
    } finally {
      setLoading(false);
    }
  }

  async function evaluateRule(ruleId: string) {
    setLoading(true);
    setError("");
    try {
      const response = await apiFetch(
        `/monitoring/alert-rules/${encodeURIComponent(ruleId)}/evaluate`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(`Alert evaluation failed (${response.status})`);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Alert evaluation failed");
      setLoading(false);
    }
  }

  async function resolveAlert(alertId: string) {
    setLoading(true);
    setError("");
    try {
      const response = await apiFetch(`/monitoring/alerts/${encodeURIComponent(alertId)}`, {
        method: "PATCH",
      });
      if (!response.ok) throw new Error(`Alert resolution failed (${response.status})`);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Alert resolution failed");
      setLoading(false);
    }
  }

  const metrics = data?.metrics ?? {};
  const display = (key: string) => {
    const item = metrics[key] as Metric | undefined;
    if (!item || item.status === "UNAVAILABLE" || item.value === null)
      return "Unavailable";
    return `${item.value} (n=${item.sample_count}${item.status === "PARTIAL" ? ", partial" : ""})`;
  };
  const latency = metrics.latency_ms as Record<string, unknown> | undefined;

  return (
    <main className="content">
      <header className="page-header">
        <div>
          <div className="eyebrow">OPERATIONS</div>
          <h1>Production monitoring</h1>
          <p>Metrics and alerts derived from stored runs and telemetry.</p>
        </div>
        <button className="refresh-button" onClick={() => void load()} disabled={loading}>
          Refresh
        </button>
      </header>
      <section className="monitor-filter panel">
        <label>
          Project ID
          <input
            value={project}
            onChange={(event) => setProject(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void load();
            }}
            placeholder="Project ID"
          />
        </label>
        <button className="text-button" onClick={() => void load()} disabled={loading}>
          Load monitoring
        </button>
      </section>
      {error && <div className="notice error-notice">{error}</div>}
      {!data && !error && (
        <div className="empty-state">Select a project to inspect metrics and alerts.</div>
      )}
      {data && (
        <>
          <div className="monitor-summary">
            <strong>{data.run_count}</strong>
            <span>stored runs in scope</span>
            <span>Latency uses nearest rank and the configured minimum sample count.</span>
          </div>
          <section className="monitor-grid">
            {[
              ["Successes", "success_count"],
              ["Failures", "failure_count"],
              ["Timeouts", "timeout_count"],
              ["Retries", "retry_count"],
              ["Tool failures", "tool_failure_count"],
              ["Model failures", "model_failure_count"],
              ["Policy violations", "policy_violation_count"],
              ["Model calls", "model_calls"],
              ["Tool calls", "tool_calls"],
              ["Tokens", "tokens"],
              ["Verified cost", "verified_cost"],
            ].map(([label, key]) => (
              <article className="monitor-metric" key={key}>
                <span>{label}</span>
                <strong>{display(key)}</strong>
              </article>
            ))}
          </section>
          <section className="panel monitor-panel">
            <h2>Latency</h2>
            <p>
              p50 {String(latency?.p50 ?? "Unavailable")} ms · p95{" "}
              {String(latency?.p95 ?? "Unavailable")} ms · p99{" "}
              {String(latency?.p99 ?? "Unavailable")} ms · samples{" "}
              {String(latency?.sample_count ?? 0)} / minimum{" "}
              {String(latency?.minimum_samples ?? "—")}
            </p>
          </section>
          <section className="panel monitor-panel">
            <h2>Budgets</h2>
            {data.budgets.length ? (
              data.budgets.map((budget) => (
                <article key={budget.id}>
                  <h3>{budget.name}</h3>
                  {Object.entries(budget.usage).map(([key, item]) => (
                    <p key={key}>
                      {key}: {item.value ?? "Unavailable"} / {item.limit} ·{" "}
                      {item.exceeded === null
                        ? item.status
                        : `${item.exceeded ? "EXCEEDED" : "within limit"} (${item.exceeded_runs ?? 0} runs)`}
                    </p>
                  ))}
                </article>
              ))
            ) : (
              <p className="muted">No budgets configured.</p>
            )}
          </section>
          <section className="panel monitor-panel">
            <h2>Alert rules</h2>
            {rules.length ? (
              rules.map((rule) => (
                <article key={rule.id}>
                  <strong>{rule.name}</strong>
                  <p>
                    {rule.metric} {rule.operator} {rule.threshold} · {rule.window_minutes} min · min n={rule.minimum_samples} · {rule.severity} · {rule.destination.type}
                  </p>
                  <button className="text-button" disabled={loading} onClick={() => void evaluateRule(rule.id)}>
                    Evaluate now
                  </button>
                </article>
              ))
            ) : (
              <p className="muted">No alert rules configured.</p>
            )}
          </section>
          <section className="panel monitor-panel">
            <h2>Alert occurrences</h2>
            {alerts.length ? (
              alerts.map((alert) => (
                <article key={alert.id}>
                  <strong>
                    {alert.status}{alert.suppressed ? " · cooldown suppressed" : ""}
                  </strong>
                  <div className="mono">{alert.fingerprint}</div>
                  <pre>{JSON.stringify(alert.evidence, null, 2)}</pre>
                  <small>{alert.deliveries.length} delivery attempt(s)</small>
                  {alert.status === "OPEN" && (
                    <button className="text-button" disabled={loading} onClick={() => void resolveAlert(alert.id)}>
                      Resolve
                    </button>
                  )}
                </article>
              ))
            ) : (
              <p className="muted">No alert occurrences.</p>
            )}
          </section>
        </>
      )}
    </main>
  );
}
