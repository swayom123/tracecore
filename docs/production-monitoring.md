# Production monitoring

TraceCore monitoring reads stored `Run`, `Span`, `TraceEvent`, `PolicyViolation`, and derived `AgentAnalysis` records. It does not generate telemetry or call models.

## Metrics and availability

Use `GET /monitoring/metrics?project_id=...` with optional environment, application, agent, agent version, and timestamp bounds. Project scope is mandatory. Success and failure use explicit run status (`SUCCEEDED` and `FAILED`). Timeouts require `metadata.timeout=true` or a span `timeout=true` attribute. Retry counts use numeric `retry_count`/attempt span attributes, explicit `retry=true`, or trace event names `retry`/`*.retry`; attributes and events on one span are combined using the larger count to avoid double counting. Model/tool failures count LLM/TOOL spans with `ERROR`, `FAILED`, or `TIMEOUT` status. Policy violations count stored violations attached to selected runs.

Token totals use recorded run `token_usage.total_tokens`, falling back to `total_tokens` attributes on LLM spans. Verified cost uses only run metadata with both `cost_verified: true` and numeric `verified_cost`; legacy `estimated_cost` is excluded. Missing metrics return `status: UNAVAILABLE`, a null value, and sample counts where applicable. Partial telemetry returns `PARTIAL` with the number of contributing runs. Critical failure rate is available only for runs with explicit boolean `metadata.critical` classification, and alert evaluation requires full classification coverage.

Latency percentiles use completed run `duration_ms` values and nearest rank: sorted sample at `ceil(p*n)-1`. The global minimum is `TRACECORE_MONITORING_PERCENTILE_MIN_SAMPLES` (default 20), and a request may override it with positive `minimum_samples`. Below the minimum, p50/p95/p99 are null and marked unavailable; observed count and minimum remain in the response.

## Run budgets

Create a project-scoped budget through `POST /monitoring/budgets` with explicit limits for `model_calls`, `tool_calls`, `tokens`, `duration_ms`, `verified_cost`, and/or `retries`. Optional scope fields narrow the target population. `GET /runs/{run_id}/budgets` reports each applicable limit against that run. Missing usage, including unverified cost or unrecorded retries, is unavailable and never interpreted as within budget. Aggregate metrics include observed run counts and exceeded run counts for each configured budget.

## Alert rules

Create rules through `POST /monitoring/alert-rules`. Every rule requires an explicit metric, comparison operator, threshold, time window, sample minimum, severity, and destination. Supported metrics are failure rate, critical failure rate (explicit classification only), runaway loop event count from stored agent analytics, policy violation count, verified cost total, and p50/p95/p99 latency. Rules are evaluated on demand with `POST /monitoring/alert-rules/{id}/evaluate`; Phase 9 has no scheduler. Missing or insufficient measurements do not fire an alert.

Each firing persists an occurrence with evidence and a stable rule/group fingerprint. Cooldown does not overwrite history: suppressed occurrences are stored and linked to the active alert group. Occurrences remain queryable after resolution; `PATCH /monitoring/alerts/{id}` resolves one occurrence. A configured grouping dimension is included in the fingerprint when the matching dimension is specified in rule scope.

Destinations are `console`, `database`, and `webhook`. Webhooks use a 3 second timeout, at most two attempts, and an `Idempotency-Key` derived from the fingerprint. Each attempt is stored separately. Logs and API rule responses omit webhook URLs; delivery failures store only exception class and response status, never response bodies or credentials. Configure webhook endpoints only for trusted destinations and protect their network route at deployment time.

## API and CLI

- `GET /monitoring/metrics`
- `POST` / `GET /monitoring/budgets`
- `GET /runs/{run_id}/budgets`
- `POST` / `GET /monitoring/alert-rules`
- `POST /monitoring/alert-rules/{id}/evaluate`
- `GET /monitoring/alerts?project_id=...`
- `PATCH /monitoring/alerts/{occurrence_id}` to resolve

CLI inspection uses `tracecore monitor metrics --project-id PROJECT`, `tracecore monitor budgets --project-id PROJECT`, `tracecore monitor rules --project-id PROJECT`, and `tracecore monitor alerts --project-id PROJECT` (add `--evaluate` to evaluate rules).

## Limits

There is no authentication layer, scheduled alert evaluator, destination management UI, deployment gate, or external destination beyond the three listed. Criticality requires explicit run metadata. Retry detection depends on recorded retry fields/events. Cost remains unavailable unless upstream instrumentation records a verified amount. Webhook URL validation accepts HTTP(S) URLs; production egress policy should restrict destinations.
