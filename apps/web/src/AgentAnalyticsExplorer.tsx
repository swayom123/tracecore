import { useState, type FormEvent } from "react";
import { apiFetch } from "./http";

interface Aggregate {
  project_id: string;
  run_count: number;
  groups: Array<{
    application_id: string;
    agent_name: string | null;
    agent_version: string | null;
    run_count: number;
    tool_usage_status: string;
    tool_usage: Array<{ tool_name: string; calls: number; succeeded: number; failed: number; outcome_unavailable: number }>;
    retry_status: string;
    retry_count: number | null;
    handoff_status: string;
    handoff_count: number | null;
  }>;
}

interface RunAnalysis {
  id: string;
  analysis: {
    run_id: string;
    trace_id: string;
    retry_analysis: { status: string; count: number | null; evidence: Array<Record<string, unknown>> };
    handoffs: { status: string; relationships: Array<Record<string, unknown>> };
    tool_usage_status: string;
    tool_usage: Array<{ tool_name: string; calls: number; succeeded: number; failed: number; outcome_unavailable: number }>;
    loop_detection: { status: string; step_count: number | null; step_count_status: string; events: Array<Record<string, unknown>> };
  };
}

export default function AgentAnalyticsExplorer() {
  const [project, setProject] = useState("");
  const [runId, setRunId] = useState("");
  const [aggregate, setAggregate] = useState<Aggregate | null>(null);
  const [analysis, setAnalysis] = useState<RunAnalysis | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const queryAggregate = async (event: FormEvent) => {
    event.preventDefault();
    setError(""); setAnalysis(null); setBusy(true);
    try {
      const response = await apiFetch(`/agent-analytics?project_id=${encodeURIComponent(project)}`);
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
      setAggregate(body as Aggregate);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load agent analytics"); }
    finally { setBusy(false); }
  };

  const analyzeRun = async () => {
    setError(""); setBusy(true);
    try {
      const response = await apiFetch(`/runs/${encodeURIComponent(runId)}/agent-analytics`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
      setAnalysis(body as RunAnalysis);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to analyze run"); }
    finally { setBusy(false); }
  };

  return <main className="content">
    <header className="page-header"><div><div className="eyebrow">AGENT / STORED TELEMETRY</div><h1>Agent Analytics</h1>
      <p>Inspect tool outcomes, recorded retries and handoffs, and deterministic loop signals.</p></div></header>
    {error && <div role="alert" className="notice error-notice">{error}</div>}
    <section className="runs-card agent-analytics-card">
      <form className="agent-analytics-form" onSubmit={(event) => void queryAggregate(event)}>
        <label>Project ID<input required value={project} onChange={(event) => setProject(event.target.value)} placeholder="Project ID" /></label>
        <button className="refresh-button" disabled={busy}>Query usage</button>
      </form>
      {aggregate && <>
        <div className="table-heading"><div><h2>Agent and tool usage</h2><p>{aggregate.run_count} stored runs in project {aggregate.project_id}</p></div></div>
        <div className="table-scroll"><table><thead><tr><th>APPLICATION</th><th>AGENT / VERSION</th><th>RUNS</th><th>TOOLS</th><th>RETRIES</th><th>HANDOFFS</th></tr></thead><tbody>{aggregate.groups.map((group) => <tr key={`${group.application_id}:${group.agent_name}:${group.agent_version}`}><td className="mono">{group.application_id}</td><td>{group.agent_name ?? "Unavailable"}<small className="muted">{group.agent_version ?? "Version unavailable"}</small></td><td>{group.run_count}</td><td>{group.tool_usage_status === "UNAVAILABLE" ? "Unavailable" : `${group.tool_usage_status === "PARTIAL" ? "Partial · " : ""}${group.tool_usage.map((tool) => `${tool.tool_name}: ${tool.calls} (${tool.succeeded} ok, ${tool.failed} failed, ${tool.outcome_unavailable} unknown)`).join(" · ")}`}</td><td>{group.retry_status === "AVAILABLE" ? group.retry_count : group.retry_status}</td><td>{group.handoff_status === "AVAILABLE" ? group.handoff_count : group.handoff_status}</td></tr>)}</tbody></table></div>
        {!aggregate.groups.length && <div className="empty-state">No runs match this project.</div>}
      </>}
    </section>
    <section className="runs-card agent-analytics-card">
      <div className="table-heading"><div><h2>Analyze a run</h2><p>Creates a persisted analysis record from this run’s stored spans and events.</p></div></div>
      <div className="agent-analytics-form"><label>Run ID<input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="Ingested run ID" /></label><button className="refresh-button" disabled={busy || !runId} onClick={() => void analyzeRun()}>Analyze run</button></div>
      {analysis && <div className="agent-run-analysis"><div className="mono">Analysis {analysis.id} · Run {analysis.analysis.run_id} · Trace {analysis.analysis.trace_id}</div>
        <h3>Tool calls</h3><pre>{JSON.stringify(analysis.analysis.tool_usage, null, 2)}</pre>
        <h3>Retries · {analysis.analysis.retry_analysis.status}</h3><pre>{JSON.stringify(analysis.analysis.retry_analysis, null, 2)}</pre>
        <h3>Handoffs · {analysis.analysis.handoffs.status}</h3><pre>{JSON.stringify(analysis.analysis.handoffs, null, 2)}</pre>
        <h3>Loop detection · {analysis.analysis.loop_detection.status}</h3><pre>{JSON.stringify(analysis.analysis.loop_detection, null, 2)}</pre>
      </div>}
    </section>
  </main>;
}
