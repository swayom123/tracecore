"""Send one locally executed instrumented workflow to a running TraceCore API."""

import os
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

import httpx


def now() -> datetime:
    return datetime.now(UTC)


def main() -> None:
    base_url = os.environ.get("TRACECORE_DEMO_API_URL", "http://127.0.0.1:8000").rstrip("/")
    run_id, trace_id = str(uuid4()), str(uuid4())
    started = now()
    root_started = now()
    root_tick = perf_counter()
    child_started = now()
    child_tick = perf_counter()
    # Represents real work in the demonstration process, with wall-clock time recorded below.
    result = "TraceCore stores observed execution with an explicit parent-child relationship."
    child_elapsed = (perf_counter() - child_tick) * 1000
    child_ended = now()
    root_elapsed = (perf_counter() - root_tick) * 1000
    root_ended = now()
    completed = now()
    trace = {
        "run": {
            "run_id": run_id,
            "project_id": "tracecore-local-demo",
            "project_name": "TraceCore local demo",
            "application_id": "local-agent",
            "application_name": "Local instrumented application",
            "environment": "development",
            "agent_name": "local-research-agent",
            "agent_version": "demo-1",
            "workflow_name": "local-knowledge-summary",
            "trace_id": trace_id,
            "status": "SUCCEEDED",
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "input": {"topic": "Trace observability"},
            "output": {"summary": result},
            "metadata": {"demo": True},
        },
        "root_span_id": "agent-root-" + run_id,
        "spans": [
            {
                "id": "agent-root-" + run_id,
                "span_type": "AGENT",
                "name": "ResearchAgent",
                "started_at": root_started.isoformat(),
                "ended_at": root_ended.isoformat(),
                "status": "OK",
                "attributes": {"agent.version": "demo-1", "duration_observed_ms": root_elapsed},
            },
            {
                "id": "summary-step-" + run_id,
                "parent_span_id": "agent-root-" + run_id,
                "span_type": "CUSTOM",
                "name": "CreateSummary",
                "started_at": child_started.isoformat(),
                "ended_at": child_ended.isoformat(),
                "status": "OK",
                "input": {"topic": "Trace observability"},
                "output": {"summary": result},
                "attributes": {"duration_observed_ms": child_elapsed},
            },
        ],
    }
    response = httpx.post(f"{base_url}/v1/traces", json=trace, timeout=10)
    response.raise_for_status()
    stored = httpx.get(f"{base_url}/traces/{trace_id}", timeout=10)
    stored.raise_for_status()
    result_data = stored.json()
    assert result_data["run"]["id"] == run_id
    assert result_data["trace"]["trace_id"] == trace_id
    parents = {span["id"]: span["parent_span_id"] for span in result_data["spans"]}
    assert parents["summary-step-" + run_id] == "agent-root-" + run_id
    print(f"Verified stored run {run_id} / trace {trace_id}; inspect: {base_url}/traces/{trace_id}")


if __name__ == "__main__":
    main()
