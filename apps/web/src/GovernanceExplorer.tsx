import { FormEvent, useCallback, useEffect, useState } from "react";
import { apiFetch } from "./http";

type Policy = {
  id: string; project_id: string; name: string; version: number;
  conditions: Array<{ field: string; operator: string; value?: unknown }>;
  effect: string; severity: string;
};
type Violation = {
  id: string; run_id: string; policy_id: string; policy_version: number;
  effect: string; severity: string; status: string; review_required: boolean;
  evidence: Array<Record<string, unknown>>; policy_snapshot: Record<string, unknown>;
  waiver: { actor: string; reason: string; expires_at: string; expired: boolean } | null;
};
type Transition = { from_status: string | null; to_status: string; actor: string; reason: string; created_at: string; expires_at: string | null };
type Evaluation = {
  id: string; policy_id: string; policy_version: number; result: string;
  decision: string; evidence: Array<Record<string, unknown>>;
  policy_snapshot: Record<string, unknown>; violation: Violation | null;
};

const API = import.meta.env.VITE_TRACECORE_API_URL ?? "";
const fields = ["environment", "agent_name", "agent_version", "run_status", "logging_mode", "model", "prompt_version", "span_status", "tool_name"];
const operators = ["eq", "neq", "in", "not_in", "exists", "not_exists"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`${API}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export default function GovernanceExplorer() {
  const [projectId, setProjectId] = useState("");
  const [runId, setRunId] = useState("");
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [violations, setViolations] = useState<Violation[]>([]);
  const [evaluations, setEvaluations] = useState<Evaluation[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [field, setField] = useState("environment");
  const [operator, setOperator] = useState("eq");
  const [value, setValue] = useState("");
  const [effect, setEffect] = useState("WARN");
  const [violationId, setViolationId] = useState("");
  const [nextStatus, setNextStatus] = useState("ACKNOWLEDGED");
  const [actor, setActor] = useState("");
  const [reason, setReason] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [history, setHistory] = useState<Record<string, Transition[]>>({});

  const refresh = useCallback(async () => {
    if (!projectId.trim()) return;
    setBusy(true); setError("");
    try {
      const [policyResult, violationResult] = await Promise.all([
        request<{ policies: Policy[] }>(`/policies?project_id=${encodeURIComponent(projectId)}`),
        request<{ violations: Violation[] }>(`/violations?project_id=${encodeURIComponent(projectId)}`),
      ]);
      setPolicies(policyResult.policies);
      setViolations(violationResult.violations);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load governance data"); }
    finally { setBusy(false); }
  }, [projectId]);

  useEffect(() => { void refresh(); }, [refresh]);

  const createPolicy = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    try {
      let conditionValue: unknown = value;
      if (["in", "not_in"].includes(operator)) conditionValue = value.split(",").map((item) => item.trim()).filter(Boolean);
      const condition: Record<string, unknown> = { field, operator };
      if (!["exists", "not_exists"].includes(operator)) condition.value = conditionValue;
      await request<Policy>("/policies", { method: "POST", body: JSON.stringify({
        project_id: projectId, name, conditions: [condition], effect, severity: "WARN",
      }) });
      setName(""); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to create policy"); }
  };

  const checkRun = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    try {
      const result = await request<{ evaluations: Evaluation[] }>(`/runs/${encodeURIComponent(runId)}/policy-check`, {
        method: "POST", body: JSON.stringify({}),
      });
      setEvaluations(result.evaluations); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to evaluate policies"); }
  };

  const transition = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    try {
      const body: Record<string, string> = { status: nextStatus, actor, reason };
      if (nextStatus === "WAIVED") body.expires_at = new Date(expiresAt).toISOString();
      await request<Violation>(`/violations/${encodeURIComponent(violationId)}`, { method: "PATCH", body: JSON.stringify(body) });
      setViolationId(""); setReason(""); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to update violation"); }
  };

  return <main className="content">
    <header className="page-header"><div><div className="eyebrow">GOVERNANCE</div><h1>Policies &amp; violations</h1>
      <p>Evaluate explicit, versioned rules against stored run telemetry. Checks do not block runs that have already completed.</p></div>
      <button className="refresh-button" onClick={() => void refresh()} disabled={busy}>↻ <span>Refresh</span></button></header>
    {error && <div className="error-banner" role="alert">{error}</div>}
    <section className="panel">
      <h2>Project scope</h2>
      <label>Project ID<input value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="project id" /></label>
    </section>
    <div className="governance-grid">
      <section className="panel">
        <h2>Create policy version</h2>
        <form className="governance-form" onSubmit={(event) => void createPolicy(event)}>
          <label>Name<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>Condition field<select value={field} onChange={(event) => setField(event.target.value)}>{fields.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label>Operator<select value={operator} onChange={(event) => setOperator(event.target.value)}>{operators.map((item) => <option key={item}>{item}</option>)}</select></label>
          {!['exists', 'not_exists'].includes(operator) && <label>Value{['in', 'not_in'].includes(operator) ? " (comma separated)" : ""}<input required value={value} onChange={(event) => setValue(event.target.value)} /></label>}
          <label>Effect<select value={effect} onChange={(event) => setEffect(event.target.value)}>{["ALLOW", "WARN", "BLOCK", "REQUIRE_REVIEW"].map((item) => <option key={item}>{item}</option>)}</select></label>
          <button className="primary-button" type="submit" disabled={!projectId.trim() || !name.trim()}>Create version</button>
        </form>
      </section>
      <section className="panel">
        <h2>Check a stored run</h2>
        <form className="governance-form" onSubmit={(event) => void checkRun(event)}>
          <label>Run ID<input required value={runId} onChange={(event) => setRunId(event.target.value)} /></label>
          <button className="primary-button" type="submit">Evaluate project policies</button>
        </form>
        {evaluations.map((item) => <article className="governance-record" key={item.id}>
          <strong>{item.policy_snapshot?.name as string || item.policy_id} v{item.policy_version}</strong>
          <span>{item.result} · {item.decision}</span>
          <pre>{JSON.stringify(item.evidence, null, 2)}</pre>
        </article>)}
      </section>
    </div>
    <section className="panel">
      <h2>Policy versions</h2>
      {!policies.length ? <p className="empty-state">No policies in this project.</p> : <div className="governance-list">{policies.map((item) => <article className="governance-record" key={item.id}>
        <strong>{item.name} <small>v{item.version}</small></strong><span>{item.effect} · {item.severity}</span><code>{JSON.stringify(item.conditions)}</code>
      </article>)}</div>}
    </section>
    <section className="panel">
      <h2>Violations</h2>
      {!violations.length ? <p className="empty-state">No policy violations have been recorded.</p> : <div className="governance-list">{violations.map((item) => <article className="governance-record" key={item.id}>
        <strong>{item.effect} · {item.severity} · {item.status}</strong><span>Run {item.run_id} · policy v{item.policy_version}{item.review_required ? " · review required" : ""}</span>
        <code>{JSON.stringify(item.evidence)}</code>{item.waiver && <span>Waived by {item.waiver.actor}; expires {item.waiver.expires_at}{item.waiver.expired ? " (expired)" : ""}</span>}
        <button className="text-button" onClick={() => {
          setViolationId(item.id);
          setNextStatus(item.status === "OPEN" ? "ACKNOWLEDGED" : "OPEN");
          void request<{ history: Transition[] }>(`/violations/${encodeURIComponent(item.id)}/history`)
            .then((result) => setHistory((current) => ({ ...current, [item.id]: result.history })))
            .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Unable to load violation history"));
        }}>Inspect history / select</button>
        {history[item.id]?.map((transition, index) => <span key={`${item.id}-${index}`}>{transition.from_status ?? "CREATED"} → {transition.to_status} by {transition.actor}: {transition.reason}</span>)}
      </article>)}</div>}
      <form className="governance-form transition-form" onSubmit={(event) => void transition(event)}>
        <h3>Record a violation transition</h3>
        <label>Violation ID<input required value={violationId} onChange={(event) => setViolationId(event.target.value)} /></label>
        <label>State<select value={nextStatus} onChange={(event) => setNextStatus(event.target.value)}>{["OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"].map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>Actor<input required value={actor} onChange={(event) => setActor(event.target.value)} /></label>
        <label>Reason<input required value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        {nextStatus === "WAIVED" && <label>Waiver expires<input required type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></label>}
        <button className="primary-button" type="submit">Save transition</button>
      </form>
    </section>
  </main>;
}
