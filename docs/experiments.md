# Experiments

TraceCore experiments compare named candidate configurations against one explicit baseline using **already ingested runs**. Comparison does not execute agents, rerun prompts, or call model providers. A candidate configuration is descriptive, versioned metadata (for example a prompt version, model name, temperature, and code reference); the run mappings identify the actual executions being measured.

## Reproducibility contract

An experiment definition is immutable and versioned by `(project_id, name, version)`. Creating a definition with the same project and name creates the next version. Each version snapshots:

- the exact dataset ID and version (dataset IDs represent immutable versions)
- evaluator IDs, versions, types, and configurations
- candidate IDs, names, redacted configuration metadata, and one stored run ID for every dataset case
- the baseline candidate ID and explicit selection criteria

Each comparison is a separate persisted `ExperimentRun`. It repeats the experiment, dataset, evaluator, candidate, mapping, and criteria snapshots and persists one result per candidate, case, and evaluator. Definitions and runs can therefore be inspected after later versions are added. Reproducibility still depends on the integrity and retention of the referenced stored traces; TraceCore does not preserve executable agent code or recreate a provider request.

Candidate run mappings must cover the dataset's case IDs exactly and every referenced run must exist in the same project. This phase does not require the run to carry the candidate configuration itself, so users must map runs accurately. Configuration metadata is redacted with TraceCore's default credential redactor before storage.

## Comparison metrics

The comparison reports deterministic evaluation pass rate (passed divided by passed plus failed evaluator results; unavailable results are excluded), failed run count (`Run.status == FAILED`), mean run latency, total recorded cost, and tool span count. Latency is unavailable if any mapped run has no duration. Cost is unavailable unless every mapped run has recorded cost; no pricing is inferred. Tool usage counts stored spans whose type is `TOOL`.

Per-evaluator counts for passed, failed, and unavailable results are also included for each candidate. Individual results preserve case ID, mapped run ID, evaluator ID and version, status, reason, and details.

Every experiment must define one or more criteria. Supported metrics are `evaluation_pass_rate`, `failure_count`, `latency_ms`, `recorded_cost`, and `tool_calls`. Higher is the preferred direction only for evaluation pass rate; the other metrics require lower. `max_regression` is the allowed adverse delta from baseline, in the metric's native units (fraction for pass rate, count for failures/tool calls, milliseconds for latency, and recorded cost units for cost). A criterion's severity is `WARN` or `FAIL`.

Each candidate is compared with the baseline independently. A missing criterion metric is `UNAVAILABLE` and yields `WARN`. A regression within the configured bound passes. A regression beyond it uses the criterion severity. The comparison status is the worst candidate/criterion status: `PASS`, `WARN`, or `FAIL`. The API always returns `winner: null`; it does not rank candidates or select a winner from a single metric.

## API

- `POST /experiments` creates the next immutable definition version.
- `GET /experiments?project_id=...&limit=...&offset=...` lists definitions.
- `GET /experiments/{experiment_id}` inspects a definition and its snapshots.
- `POST /experiments/{experiment_id}/compare` runs a data-only comparison and persists results.
- `GET /experiments/{experiment_id}/runs` lists saved comparison runs.
- `GET /experiment-runs/{experiment_run_id}` returns snapshots, metric and evaluator breakdowns, criteria decisions, and per-case evaluator results.

Creation request example:

```json
{
  "project_id": "support-project",
  "name": "answer-prompt",
  "dataset_id": "dataset-version-id",
  "evaluator_ids": ["exact-match-evaluator-version-id"],
  "baseline_candidate_id": "prompt-a",
  "candidates": [
    {
      "id": "prompt-a",
      "name": "Prompt A",
      "configuration": {"prompt_version": "support-v12", "model": "model-a"},
      "case_runs": {"case-1": "baseline-run-1"}
    },
    {
      "id": "prompt-b",
      "name": "Prompt B",
      "configuration": {"prompt_version": "support-v13", "model": "model-a"},
      "case_runs": {"case-1": "candidate-run-1"}
    }
  ],
  "criteria": [
    {"metric": "evaluation_pass_rate", "direction": "HIGHER", "max_regression": 0.02, "severity": "FAIL"},
    {"metric": "latency_ms", "direction": "LOWER", "max_regression": 100, "severity": "WARN"},
    {"metric": "recorded_cost", "direction": "LOWER", "max_regression": 0.01, "severity": "WARN"}
  ]
}
```

## CLI

The CLI reads a JSON object with the same create contract:

```sh
tracecore experiment create --file experiment.json
tracecore experiment list --project-id support-project
tracecore experiment compare <experiment-id>
tracecore experiment show <experiment-run-id>
```

## Web console

The Experiments view lists definitions, shows candidate configuration and mappings, displays criteria, runs comparisons, and inspects saved comparisons with candidate metrics, criteria decisions, and case-level evaluator results. It does not provide experiment authoring; definitions are created through the API or CLI.

## Limitations

- There is no agent execution, model-provider integration, randomization, traffic splitting, or statistical significance analysis.
- Candidate configuration metadata is user-supplied and is not verified against the mapped run's recorded model/prompt attributes.
- Aggregated latency is an arithmetic mean, not a percentile; no significance or confidence claims are made.
- Failed run count counts only the persisted `FAILED` run status. `PARTIAL` and failed spans are visible through the case mappings and traces but do not increment this metric.
- Cost comparison is available only when cost was recorded for all mapped runs, and it uses the stored cost values as-is.
- Criteria thresholds are explicit but have no automatic defaults or deployment-gate effect.
- Authentication, authorization, and project isolation beyond project ID validation remain outside the current platform implementation.
