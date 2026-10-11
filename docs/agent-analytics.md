# Agent-specific analytics

Agent analytics derive summaries and signals from ingested runs, spans, and events. They do not execute agents, modify source telemetry, or call model providers. A run analysis is persisted separately in `agent_analyses`, with the threshold configuration used and span IDs supporting the findings.

## Event and attribute contract

TraceCore uses the existing span/event contract. Relevant spans use `span_type=TOOL`, `PLANNER`, or `AGENT`. Tool outcomes use the recorded span status: `OK` is succeeded, `ERROR` or a recorded error string is failed, and `UNSET` is unavailable. Tool names come from `Span.name`.

Explicit retry instrumentation is recognized on span attributes:

- `retry_count`: number of retries for that span (non-negative integer)
- `retry_attempt` or `attempt`: one-based attempt number; retries are `max(attempt - 1, 0)`
- `retry: true`: one retry
- trace events named `retry`, `tool.retry`, `llm.retry`, or another `*.retry`: each matching event counts as one retry

When both attributes and retry events exist for one span, TraceCore uses the larger of the attribute count and number of retry events to avoid double counting. Repeated calls alone do not count as retries.

Handoffs are recognized from explicit span attributes `source_agent`, `parent_agent`, `handoff_from`, `target_agent`, or `handoff_to`; or events named `handoff`, `agent.handoff`, or `agent_handoff` with `source_agent`/`from_agent` and `target_agent`/`to_agent` attributes. TraceCore does not infer an agent handoff from span parentage alone.

## Detection semantics

`POST /runs/{run_id}/agent-analytics` computes and persists a new analysis. The optional `config` object accepts:

| Setting | Default | Signal |
|---|---:|---|
| `repeated_tool_call_threshold` | 3 | Consecutive TOOL spans with the same name; `CONSECUTIVE_TOOL_NAME_HEURISTIC` |
| `repeated_failure_threshold` | 2 | Same span name and exact error fingerprint; `EXACT_ERROR_HEURISTIC` |
| `repeated_plan_threshold` | 3 | Repeated explicit `plan_id`/`plan_hash`, or repeated planner output fingerprint; method identifies explicit vs heuristic |
| `excessive_step_count_threshold` | 20 | Non-root spans in the trace; `TRACE_STRUCTURE` |

Each signal has a configurable severity: `INFO`, `WARN`, or `ERROR`, using its corresponding `*_severity` property. Thresholds are bounded and validated by the API. Plan/error fingerprints are truncated SHA-256 values; raw error text and plan contents are not returned in loop findings. Repeated tool detection uses consecutive tool names and does not compare arguments, so it is a heuristic signal rather than proof of a runaway loop.

Missing retry or handoff instrumentation returns `UNAVAILABLE` and a null count. Aggregate data reports `AVAILABLE`, `PARTIAL`, or `UNAVAILABLE` when explicit evidence covers all, some, or none of the filtered runs. Missing tool spans likewise report `tool_usage_status=UNAVAILABLE`; recorded tool outcome `UNSET` is counted separately as `outcome_unavailable`. Step count is available when the trace has stored spans.

## API

- `POST /runs/{run_id}/agent-analytics` body: `{ "config": { ...thresholds... } }`; creates a persisted derived analysis record.
- `GET /runs/{run_id}/agent-analytics` returns the latest persisted analysis, or 404 if analysis has not been computed.
- `GET /agent-analytics?project_id=...` returns project-scoped aggregates. Optional filters: `application_id`, `agent_name`, `agent_version`, `started_after`, `started_before` (timezone-aware timestamps).

The aggregate groups by application, agent name, and registered agent version. It includes run count, tool calls and recorded outcomes, explicit retry counts, and explicit handoff counts. Tool usage status is `AVAILABLE`, `PARTIAL`, or `UNAVAILABLE` depending on whether tool spans exist for all, some, or none of the grouped runs. Unavailable and partially observed retry/handoff fields are never converted to zero.

## CLI

```sh
tracecore agent analytics run <run-id>
tracecore agent analytics run <run-id> --config thresholds.json
tracecore agent analytics show <run-id>
tracecore agent analytics aggregate --project-id <project-id> \
  --application-id <application-id> --agent-name <agent> --agent-version <version> \
  --started-after 2026-01-01T00:00:00Z
```

`thresholds.json` contains the fields in the config table. Running analytics stores a new derived record; `show` reads the most recent record.

## Privacy and limitations

Analytics responses include application/agent/tool names, span IDs, explicit handoff endpoints, and threshold results. They do not include prompt/output content or raw errors; source span attributes and events remain subject to the existing ingestion redactor. Treat names and caller-provided attributes as untrusted telemetry. Project filtering exists, but TraceCore does not yet implement authentication or authorization.

Signals are deterministic pattern matches, not model judgments. Repeated-tool and repeated-failure detection can produce false positives. Aggregate retry and handoff totals require explicit instrumentation coverage; TraceCore cannot distinguish no activity from missing instrumentation. No alerts, background recomputation, agent execution, framework auto-instrumentation, or production analytics store are implemented.
