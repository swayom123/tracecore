import { useEffect, useMemo, useState } from "react";
import {
  fetchEvaluation,
  fetchEvaluations,
  type EvaluationDetail,
  type EvaluationSummary,
} from "./evaluations";

function date(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function Badge({ value }: { value: string }) {
  return (
    <span className={`status status-${value.toLowerCase()}`}>{value}</span>
  );
}

export default function EvaluationExplorer() {
  const [evaluations, setEvaluations] = useState<EvaluationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvaluationDetail | null>(null);
  const [filter, setFilter] = useState("ALL");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchEvaluations()
      .then((body) => {
        if (active) setEvaluations(body.evaluations);
      })
      .catch((reason: unknown) => {
        if (active)
          setError(
            reason instanceof Error
              ? reason.message
              : "Unable to load evaluations",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetail(null);
    setLoading(true);
    fetchEvaluation(selectedId)
      .then((body) => {
        if (active) setDetail(body);
      })
      .catch((reason: unknown) => {
        if (active)
          setError(
            reason instanceof Error
              ? reason.message
              : "Unable to load evaluation",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedId]);

  const evaluatorNames = useMemo(
    () =>
      new Map(
        (detail?.evaluator_snapshot ?? []).map((item) => [
          item.id,
          `${item.name} v${item.version}`,
        ]),
      ),
    [detail],
  );
  const visibleResults = useMemo(
    () =>
      detail?.results.filter(
        (item) => filter === "ALL" || item.status === filter,
      ) ?? [],
    [detail, filter],
  );

  return (
    <main className="content">
      <header className="page-header">
        <div>
          <div className="eyebrow">EVALUATION / STORED RESULTS</div>
          <h1>Evaluations</h1>
          <p>
            Inspect persisted evaluator results for explicitly mapped stored
            runs.
          </p>
        </div>
        {selectedId && (
          <button
            className="refresh-button"
            onClick={() => setSelectedId(null)}
          >
            ← All evaluations
          </button>
        )}
      </header>
      {error && (
        <div role="alert" className="notice error-notice">
          Could not load evaluations: {error}
        </div>
      )}
      {selectedId ? (
        detail ? (
          <>
            <section className="evaluation-summary-card">
              <div className="evaluation-summary-heading">
                <div>
                  <div className="eyebrow">EVALUATION RUN</div>
                  <h2 className="mono">{detail.id}</h2>
                </div>
                <Badge value={detail.status} />
              </div>
              <div className="evaluation-facts">
                <span>
                  Dataset <code>{detail.dataset_id}</code> · v
                  {detail.dataset_version}
                </span>
                <span>Created {date(detail.created_at)}</span>
                <span>{detail.total_results} case/evaluator results · {detail.assessed} model assessed</span>
              </div>
              <div className="result-counts">
                <span>
                  <b>{detail.passed}</b> passed
                </span>
                <span>
                  <b>{detail.failed}</b> failed
                </span>
                <span>
                  <b>{detail.unavailable}</b> unavailable
                </span>
                <span><b>{detail.assessed}</b> model assessed</span>
              </div>
              <div className="evaluation-snapshots">
                <h3>Evaluator versions used</h3>
                {detail.evaluator_snapshot.map((item) => (
                  <div key={item.id}>
                    <b>{item.name}</b>{" "}
                    <span>
                      v{item.version} · {item.type}
                    </span>
                    <code>{item.id}</code>
                    <details className="snapshot-config">
                      <summary>Config</summary>
                      <pre>{JSON.stringify(item.config, null, 2)}</pre>
                    </details>
                  </div>
                ))}
              </div>
            </section>
            <section className="runs-card evaluation-results-card">
              <div className="table-heading">
                <div>
                  <h2>Case results</h2>
                  <p>
                    Deterministic checks and model-assisted assessments are listed separately.
                  </p>
                </div>
                <label className="result-filter">
                  Status{" "}
                  <select
                    value={filter}
                    onChange={(event) => setFilter(event.target.value)}
                  >
                    <option value="ALL">All</option>
                    <option value="FAILED">Failed</option>
                    <option value="UNAVAILABLE">Unavailable</option>
                    <option value="PASSED">Passed</option>
                    <option value="ASSESSED">Model assessed</option>
                  </select>
                </label>
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>CASE</th>
                      <th>RUN</th>
                      <th>EVALUATOR VERSION</th>
                      <th>STATUS</th>
                      <th>REASON</th>
                      <th>DETAILS</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleResults.map((item) => (
                      <tr key={item.id} className="evaluation-result-row">
                        <td className="mono">
                          <span>{item.case_id}</span>
                          <small className="muted">
                            dataset v{item.dataset_version}
                          </small>
                          {item.case && (
                            <details className="case-inspector">
                              <summary>Case data</summary>
                              <pre>{JSON.stringify(item.case, null, 2)}</pre>
                            </details>
                          )}
                        </td>
                        <td className="mono">{item.run_id ?? "—"}</td>
                        <td>
                          {evaluatorNames.get(item.evaluator_id) ??
                            item.evaluator_id}{" "}
                          <span className="muted">
                            (result v{item.evaluator_version})
                          </span>
                        </td>
                        <td>
                          <Badge value={item.status} />
                        </td>
                        <td className="result-reason">{item.reason}</td>
                        <td>
                          <details>
                            <summary>{item.status === "ASSESSED" ? "Model assessment" : "Check details"}</summary>
                            {item.status === "ASSESSED" && (
                              <p className="judge-summary">
                                {String(item.details.provider)} / {String(item.details.model)} · prompt {String(item.details.prompt_version)}<br />
                                Score {String(item.details.score)} · confidence {String(item.details.confidence)}<br />
                                {String(item.details.reason)}
                              </p>
                            )}
                            <pre>{JSON.stringify(item.details, null, 2)}</pre>
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {visibleResults.length === 0 && (
                <div className="empty-inline">
                  No results match this status.
                </div>
              )}
            </section>
          </>
        ) : loading ? (
          <div className="loading-state">Loading evaluation…</div>
        ) : null
      ) : loading ? (
        <div className="loading-state">Loading evaluations…</div>
      ) : evaluations.length === 0 ? (
        <section className="runs-card">
          <div className="empty-state">
            <div className="empty-icon">✓</div>
            <h3>No evaluations have been run.</h3>
            <p>
              Create a dataset and deterministic evaluator, then evaluate
              explicitly mapped ingested runs using the API or CLI.
            </p>
            <a href="/docs" target="_blank" rel="noreferrer">
              Open API documentation ↗
            </a>
          </div>
        </section>
      ) : (
        <section className="runs-card">
          <div className="table-heading">
            <div>
              <h2>Evaluation runs</h2>
              <p>{evaluations.length} recent persisted evaluations</p>
            </div>
            <span className="live-label">STORED RESULTS</span>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>CREATED</th>
                  <th>DATASET</th>
                  <th>VERSION</th>
                  <th>STATUS</th>
                  <th>RESULTS</th>
                  <th>EVALUATION ID</th>
                </tr>
              </thead>
              <tbody>
                {evaluations.map((item) => (
                  <tr
                    key={item.id}
                    tabIndex={0}
                    onClick={() => {
                      setError("");
                      setSelectedId(item.id);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        setError("");
                        setSelectedId(item.id);
                      }
                    }}
                  >
                    <td>{date(item.created_at)}</td>
                    <td className="mono">{item.dataset_id}</td>
                    <td>v{item.dataset_version}</td>
                    <td>
                      <Badge value={item.status} />
                    </td>
                    <td>
                      {item.passed} passed · {item.failed} failed ·{" "}
                      {item.unavailable} unavailable
                      {item.assessed > 0 ? ` · ${item.assessed} model assessed` : ""}
                    </td>
                    <td className="mono">{item.id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <footer className="page-footer">
        No aggregate quality score is calculated. Unavailable means the required
        telemetry was not recorded.
      </footer>
    </main>
  );
}
