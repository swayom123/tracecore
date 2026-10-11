# Tracing model

## Semantics

- A **run** is the caller-visible execution unit. It has stable `run_id` and `trace_id`, application/environment context, explicit status, and optional input/output references.
- A **trace** represents the workflow execution and records its root span and overall timing/status.
- A **span** is one timed operation. Its `parent_span_id` links it to the operation that initiated it. The Phase 1 API accepts the span tree as a complete trace payload.
- A **trace event** is a timestamped annotation attached to a span.

Run statuses are `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, and `PARTIAL`. Span status is `UNSET`, `OK`, or `ERROR`. Span types include agent, LLM, tool, retrieval, planner, workflow, HTTP, database, guardrail, verification, and custom.

The trace must have one root with no parent. Every non-root span points to a span in the same payload. Span IDs are unique in the trace and cycles are rejected. Timestamps must be ordered. Duration is calculated as `ended_at - started_at` in milliseconds; it is `null` if an end timestamp is absent. A completed run status needs `completed_at` and is supplied by the instrumenting application.

## Ingestion shape

```json
{
  "run": {
    "run_id": "stable-run-id",
    "project_id": "project-id",
    "application_id": "application-id",
    "environment": "development",
    "trace_id": "stable-trace-id",
    "status": "SUCCEEDED",
    "started_at": "2026-01-01T12:00:00Z",
    "completed_at": "2026-01-01T12:00:01Z"
  },
  "root_span_id": "root",
  "spans": [
    {"id": "root", "span_type": "AGENT", "name": "Agent", "started_at": "2026-01-01T12:00:00Z", "ended_at": "2026-01-01T12:00:01Z", "status": "OK"}
  ]
}
```

Unknown fields are rejected so client typos do not silently disappear. See `/docs` for the live OpenAPI schema.

