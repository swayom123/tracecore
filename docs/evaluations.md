# Evaluation Engine

TraceCore evaluates stored run telemetry against versioned test cases. It does not invoke an agent or arbitrary uploaded code. Deterministic evaluators do not call a model. Optional `MODEL_JUDGE` evaluators use the separate, explicitly configured model-based evaluation path described in [Model-based evaluation](model-based-evaluation.md). Callers explicitly map each dataset case ID to an already ingested run ID.

## Version semantics

A dataset is identified by project, name, and integer version. Creating another dataset with the same project and name creates the next immutable version and new case IDs. An evaluator is identified by project, name, and integer version. Re-registering the name creates the next version; it does not rewrite prior definitions. An evaluation stores its dataset version, a snapshot of evaluator IDs, names, versions, types, and configuration, plus per-case result rows that each record both the dataset and evaluator version.

Dataset cases pass through the configured TraceCore content redactor before storage. In `METADATA_ONLY` mode their content is omitted, and output-content evaluators return `UNAVAILABLE` rather than comparing omission markers. Evaluator configuration is operational metadata: it uses credential redaction so thresholds and rule definitions remain executable. Trace content and expected values should still be treated as sensitive. Redaction is a credential-focused foundation, not comprehensive PII detection.

## Supported deterministic evaluators

| Type | Config | Behavior |
| --- | --- | --- |
| `EXACT_MATCH` | optional `output_path` | Compares stored run output (or nested object path) to case `expected_output` using JSON value equality. |
| `REQUIRED_FIELDS` | `fields: string[]`, optional `output_path` | Checks configured top-level keys exist on an object. |
| `VALID_JSON` | optional `output_path` | Parses a string as JSON; already structured stored JSON counts as valid. |
| `REGEX_MATCH` | `pattern`, optional `output_path` | Applies a validated Python regular expression to string output. |
| `RESPONSE_LENGTH` | `min_chars` and/or `max_chars`, optional `output_path` | Checks character count of string output. |
| `CITATION_PRESENT` | optional `pattern`, optional `output_path` | Checks for a citation marker. Default recognizes bracketed markers and URLs; it does not verify the source. |
| `TASK_OUTCOME` | none | Passes only when the stored run status is explicitly `SUCCEEDED`. |
| `LATENCY_THRESHOLD` | `max_ms` | Checks stored run duration derived from timestamps. Missing duration is unavailable. |
| `COST_THRESHOLD` | `max_cost` | Checks recorded run cost. Missing verified cost is unavailable, never estimated here. |
| `TOOL_CALL_CONSTRAINTS` | `min_calls`, `max_calls`, `allowed_tools`, `required_tools` | Checks recorded `TOOL` span names and count. At least one constraint is required. |

`output_path` is a dotted path through JSON objects, such as `answer.text`. Evaluator configs reject unsupported fields and invalid regular expressions. User-supplied regexes use a restricted safe subset: groups, alternation, unbounded wildcard expressions, and multiple repetition operators are rejected. Checks return `UNAVAILABLE` when the required telemetry was not recorded, `FAILED` when a recorded value violates the check, and `PASSED` when it satisfies it.

`EXACT_MATCH` returns `UNAVAILABLE` when the case omitted `expected_output`; an explicit JSON `null` is retained as an expected value. An absent candidate run output remains unavailable because the ingestion model does not distinguish an explicitly returned null from an unrecorded output.

An evaluation summary is `FAIL` if any check fails, `WARN` if none fail and at least one is unavailable, otherwise `PASS`. Counts are kept by result status. There is no blended quality score.

## API workflow

First ingest one or more real runs using `POST /v1/traces`. Create a dataset under an existing project:

```json
{
  "project_id": "support-project",
  "name": "answer-regression",
  "description": "Support response checks",
  "cases": [
    {
      "id": "case-1",
      "input": {"question": "How do I reset my password?"},
      "expected_output": {"answer": "Use the account reset flow."}
    }
  ]
}
```

`POST /evaluations/datasets` responds with a new immutable dataset version. Create an evaluator:

```json
{
  "project_id": "support-project",
  "name": "exact-answer-v1",
  "evaluator_type": "EXACT_MATCH",
  "config": {"output_path": "answer"}
}
```

`POST /evaluations/evaluators` responds with an immutable evaluator version. Run checks by explicitly pairing a case with a stored run:

```json
{
  "dataset_id": "<dataset-id>",
  "evaluator_ids": ["<evaluator-id>"],
  "case_runs": [{"case_id": "case-1", "run_id": "<ingested-run-id>"}]
}
```

Use `GET /evaluations` to list summaries and `GET /evaluations/{id}` to inspect each case/evaluator result and reason. Unmapped dataset cases are persisted as `UNAVAILABLE` results so the result denominator is visible. Evaluation execution is synchronous in this phase.

## CLI

All CLI dataset and evaluator create commands accept JSON files matching the API request bodies:

```sh
tracecore eval dataset create --file dataset.json
tracecore eval dataset list --project-id support-project
tracecore eval evaluator create --file evaluator.json
tracecore eval evaluator list --project-id support-project
tracecore eval run --dataset-id DATASET_ID --evaluator-id EVALUATOR_ID --case-run CASE_ID=RUN_ID
tracecore eval show EVALUATION_ID
```

Repeated `--evaluator-id` and `--case-run` options are supported for one evaluation invocation.

## Limits and security

- Dataset and evaluator versions are append-only by API convention; there are no update/delete endpoints in this phase.
- API project IDs are used to prevent evaluating runs from another project, but authentication, RBAC, and organization isolation remain unimplemented.
- A run can be mapped to multiple cases; TraceCore does not check that run input matches the case input. The mapping is an explicit caller assertion.
- Deterministic string equality, regex, JSON shape, citation-marker, outcome, latency, cost, and tool-span checks do not establish answer correctness or groundedness.
- Human evaluation and custom Python evaluators are not supported. Model-based evaluation is opt-in and has separate `ASSESSED` results; see [Model-based evaluation](model-based-evaluation.md).
- Since verified model pricing is not implemented, cost threshold results normally report `UNAVAILABLE`.
