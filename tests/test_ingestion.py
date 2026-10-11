from datetime import UTC, datetime, timedelta

from tracecore.schemas import TracePayload


def sample_payload() -> dict:
    started = datetime.now(UTC)
    ended = started + timedelta(milliseconds=35)
    return {
        "run": {
            "run_id": "run-1",
            "project_id": "project-1",
            "application_id": "app-1",
            "environment": "development",
            "agent_name": "assistant",
            "agent_version": "v1",
            "trace_id": "trace-1",
            "status": "SUCCEEDED",
            "started_at": started.isoformat(),
            "completed_at": ended.isoformat(),
            "input": {"authorization": "Bearer private-value", "question": "hello"},
            "output": {"answer": "world"},
        },
        "root_span_id": "root-1",
        "spans": [
            {
                "id": "root-1",
                "span_type": "AGENT",
                "name": "Agent",
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "status": "OK",
            },
            {
                "id": "child-1",
                "parent_span_id": "root-1",
                "span_type": "TOOL",
                "name": "Lookup",
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "status": "OK",
                "attributes": {"api_key": "secret-value", "operation": "lookup"},
            },
        ],
    }


def test_ingests_and_queries_trace_with_redaction_and_derived_duration(client) -> None:
    payload = sample_payload()
    response = client.post("/v1/traces", json=payload, headers={"x-request-id": "request-test-1"})
    assert response.status_code == 202
    assert response.headers["x-request-id"] == "request-test-1"
    assert response.json()["accepted"] == ["run-1"]

    run = client.get("/runs/run-1")
    assert run.status_code == 200
    body = run.json()
    assert body["run"]["trace_id"] == "trace-1"
    assert body["run"]["input"]["authorization"] == "[REDACTED]"
    assert body["spans"][1]["parent_span_id"] == "root-1"
    assert body["spans"][1]["attributes"]["api_key"] == "[REDACTED]"
    assert 34 <= body["spans"][0]["duration_ms"] <= 36

    trace = client.get("/traces/trace-1")
    assert trace.status_code == 200
    assert trace.json()["trace"]["root_span_id"] == "root-1"
    assert client.get("/traces/trace-1/spans").json()["spans"][1]["id"] == "child-1"
    assert client.get("/runs").json()["runs"][0]["id"] == "run-1"


def test_duplicate_ingestion_is_idempotent(client) -> None:
    payload = sample_payload()
    assert client.post("/v1/traces", json=payload).status_code == 202
    assert client.post("/v1/traces", json=payload).status_code == 202
    assert len(client.get("/runs").json()["runs"]) == 1


def test_invalid_parent_is_rejected_with_actionable_error(client) -> None:
    payload = sample_payload()
    payload["spans"][1]["parent_span_id"] = "missing-parent"
    response = client.post("/v1/traces", json=payload)
    assert response.status_code == 422
    assert "unknown parent" in str(response.json()).lower()
    assert client.get("/runs").json()["runs"] == []


def test_batch_accepts_valid_and_reports_malformed_independently(client) -> None:
    valid = sample_payload()
    invalid = sample_payload()
    invalid["run"]["run_id"] = "broken-run"
    invalid["run"]["trace_id"] = "broken-trace"
    invalid["spans"][1]["parent_span_id"] = "absent"
    response = client.post("/v1/traces/batch", json={"traces": [valid, invalid]})
    assert response.status_code == 207
    assert response.json()["accepted"] == ["run-1"]
    assert response.json()["rejected"][0]["index"] == "1"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_status_requires_explicit_completed_outcome_timestamp() -> None:
    data = sample_payload()
    data["run"].pop("completed_at")
    try:
        TracePayload.model_validate(data)
    except ValueError as exc:
        assert "completed_at is required" in str(exc)
    else:
        raise AssertionError("completed run without completion time should fail validation")


def test_ingestion_rejects_oversized_stream(client, monkeypatch) -> None:
    from tracecore import api
    from tracecore.config import Settings

    monkeypatch.setattr(api, "get_settings", lambda: Settings(max_payload_bytes=1024))
    payload = sample_payload()
    payload["run"]["metadata"] = {"large_value": "x" * 2000}
    response = client.post("/v1/traces", json=payload)
    assert response.status_code == 413
    assert client.get("/runs").json()["runs"] == []
