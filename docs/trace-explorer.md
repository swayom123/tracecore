# Trace Explorer

TraceCore's Phase 3 web application is a read-only React and TypeScript client of the existing FastAPI query endpoints. It does not use generated fixtures or write to the trace store.

## Local development

1. Install and start the API as described in the [README quick start](../README.md#quick-start).
2. In another terminal, run `cd apps/web && npm ci && npm run dev`.
3. Open the Vite URL, normally `http://127.0.0.1:5173`.

The Vite development proxy forwards `/runs`, `/traces`, `/health`, and `/docs` to `http://127.0.0.1:8000`. Production hosting must route `/runs` and `/traces` to the TraceCore API on the same origin.

## Runs query

`GET /runs` returns `runs`, `limit`, `offset`, and `total`. Optional filters are `environment`, `application_id`, `agent_name`, `agent_version` (exact registered version), `status`, `started_after`, and `started_before`. Date filters must be ISO 8601 datetimes. The browser requests 25 records at a time and uses `total` for next/previous state.

## Trace inspection

Selecting a run opens `GET /traces/{trace_id}`. Trace spans are ordered into a parent/child tree from `parent_span_id`, rooted at the trace's declared `root_span_id`. The waterfall uses span start/end timestamps and duration values returned by the API. Missing timestamps remain unpositioned or show as unavailable; the client does not manufacture timing values. Selecting a span opens a details panel with its IDs, type, status, timestamps, duration, attributes, error, and persisted input/output references.

Trace content shown in the panel has already passed through the API's configured redaction service. The client does not attempt to reverse redaction or infer hidden content.

## Current limits

- The console has no login, RBAC, environment authorization, or organization isolation. Keep it on a trusted local network until those controls exist.
- Runs can be filtered by exact environment, application ID, agent name, agent version, status, and start-time bounds. Tags and date presets are not filter controls yet.
- The Runs table shows the stored agent version and ID when recorded; it does not resolve unavailable version metadata.
- The timeline does not display streaming token events, retries as a distinct marker, or cross-trace links.
- Evaluations, experiments, replay, alerts, and governance are outside this phase.
