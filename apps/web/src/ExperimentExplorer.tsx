import { useEffect, useState } from "react";
import {
  compareExperiment,
  fetchExperimentRun,
  fetchExperimentRuns,
  fetchExperiments,
  type Experiment,
  type ExperimentRun,
} from "./experiments";

function Badge({ value }: { value: string }) {
  return <span className={`status status-${value.toLowerCase()}`}>{value}</span>;
}

export default function ExperimentExplorer() {
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [selected, setSelected] = useState<Experiment | null>(null);
  const [runs, setRuns] = useState<ExperimentRun[]>([]);
  const [detail, setDetail] = useState<ExperimentRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    try { setExperiments(await fetchExperiments()); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load experiments"); }
  };
  useEffect(() => { void reload(); }, []);

  const choose = async (item: Experiment) => {
    setSelected(item); setDetail(null); setError("");
    try { setRuns((await fetchExperimentRuns(item.id)).runs); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load comparisons"); }
  };
  const inspect = async (item: ExperimentRun) => {
    try { setDetail(await fetchExperimentRun(item.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load comparison"); }
  };
  const compare = async () => {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const item = await compareExperiment(selected.id);
      setRuns((current) => [item, ...current]); setDetail(await fetchExperimentRun(item.id));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to compare experiment"); }
    finally { setBusy(false); }
  };

  return <main className="content">
    <header className="page-header"><div><div className="eyebrow">COMPARISON / STORED RUNS</div><h1>Experiments</h1>
      <p>Compare versioned candidate configurations using mapped, ingested runs and configured criteria.</p></div>
      {selected && <button className="refresh-button" onClick={() => { setSelected(null); setDetail(null); }}>← All experiments</button>}
    </header>
    {error && <div role="alert" className="notice error-notice">{error}</div>}
    {!selected ? <section className="runs-card"><div className="table-scroll"><table><thead><tr><th>NAME</th><th>VERSION</th><th>DATASET</th><th>CANDIDATES</th><th>CRITERIA</th><th>CREATED</th></tr></thead>
      <tbody>{experiments.map((item) => <tr key={item.id}><td><button className="text-button" onClick={() => void choose(item)}>{item.name}</button><small className="muted mono">{item.id}</small></td><td>v{item.version}</td><td className="mono">{item.dataset_id} · v{item.dataset_version}</td><td>{item.candidates.length}</td><td>{item.criteria.length}</td><td>{new Date(item.created_at).toLocaleString()}</td></tr>)}</tbody></table>
      {!experiments.length && <div className="empty-state">No experiments have been defined.</div>}</div></section> : <>
      <section className="runs-card experiment-definition"><div className="table-heading"><div><h2>{selected.name} · v{selected.version}</h2><p>Dataset {selected.dataset_id} · v{selected.dataset_version}; baseline {selected.baseline_candidate_id}</p></div><button className="refresh-button" disabled={busy} onClick={() => void compare()}>{busy ? "Comparing…" : "Run comparison"}</button></div>
        <h3>Candidate configurations</h3><div className="experiment-candidates">{selected.candidates.map((candidate) => <details key={candidate.id}><summary>{candidate.name} <span className="mono muted">{candidate.id}</span>{candidate.id === selected.baseline_candidate_id ? " · baseline" : ""}</summary><pre>{JSON.stringify(candidate.configuration, null, 2)}</pre><small>{Object.keys(candidate.case_runs).length} mapped cases</small></details>)}</div>
        <h3>Selection criteria</h3><ul>{selected.criteria.map((criterion) => <li key={criterion.metric}>{criterion.metric}: {criterion.direction}, max regression {criterion.max_regression} ({criterion.severity})</li>)}</ul>
      </section>
      {detail && <section className="runs-card experiment-results"><div className="table-heading"><div><h2>Comparison result</h2><p className="mono">{detail.id} · experiment v{detail.experiment_version} · dataset v{detail.dataset_version}</p></div><Badge value={detail.status}/></div>
        <div className="table-scroll"><table><thead><tr><th>CANDIDATE</th><th>STATUS</th><th>EVALUATION PASS RATE</th><th>FAILED RUNS</th><th>MEAN LATENCY</th><th>RECORDED COST</th><th>TOOL CALLS</th></tr></thead><tbody>{detail.comparison.candidates.map((candidate) => <tr key={candidate.candidate_id}><td>{candidate.candidate_name}<small className="muted mono">{candidate.candidate_id}</small></td><td><Badge value={candidate.status}/></td><td>{formatMetric(candidate.metrics.evaluation_pass_rate, "%")}</td><td>{formatMetric(candidate.metrics.failure_count)}</td><td>{formatMetric(candidate.metrics.latency_ms, " ms")}</td><td>{formatMetric(candidate.metrics.recorded_cost)}</td><td>{formatMetric(candidate.metrics.tool_calls)}</td></tr>)}</tbody></table></div>
        <div className="experiment-criteria">{detail.comparison.candidates.filter((candidate) => candidate.criteria && Object.keys(candidate.criteria).length).map((candidate) => <section key={candidate.candidate_id}><h3>{candidate.candidate_name} vs baseline</h3>{Object.entries(candidate.criteria).map(([metric, item]) => <p key={metric}><b>{metric}</b>: {item.status}{item.reason ? ` · ${item.reason}` : item.delta !== null ? ` · delta ${item.delta}` : ""}</p>)}</section>)}</div>
        <h3>Per case and evaluator</h3><div className="table-scroll"><table><thead><tr><th>CANDIDATE</th><th>CASE</th><th>RUN</th><th>EVALUATOR</th><th>RESULT</th><th>REASON</th></tr></thead><tbody>{(detail.results ?? []).map((result) => <tr key={result.id}><td>{result.candidate_id}</td><td className="mono">{result.case_id}</td><td className="mono">{result.run_id}</td><td className="mono">{result.evaluator_id} v{result.evaluator_version}</td><td><Badge value={result.status}/></td><td>{result.reason}</td></tr>)}</tbody></table></div>
      </section>}
      <section className="runs-card"><div className="table-heading"><div><h2>Saved comparisons</h2><p>Each comparison is a persisted snapshot and can be inspected again.</p></div></div><div className="comparison-list">{runs.map((item) => <button key={item.id} className="comparison-link" onClick={() => void inspect(item)}><span className="mono">{item.id}</span><Badge value={item.status}/><small>{new Date(item.created_at).toLocaleString()}</small></button>)}{!runs.length && <div className="empty-state">No comparisons have been run for this experiment.</div>}</div></section>
    </>}
  </main>;
}

function formatMetric(value: unknown, suffix = ""): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  const display = suffix === "%" ? value * 100 : value;
  return `${display.toFixed(suffix === "%" ? 1 : 2)}${suffix}`;
}
