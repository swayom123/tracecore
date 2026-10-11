from datetime import UTC, datetime, timedelta


def telemetry(run_id: str = "analytics-run", *, project="analytics-project", version="v9") -> dict:
    started = datetime.now(UTC)

    def span_time(offset: int) -> str:
        return (started + timedelta(milliseconds=offset)).isoformat()

    spans = [
        {
            "id": "root",
            "span_type": "AGENT",
            "name": "ResearchAgent",
            "started_at": span_time(0),
            "ended_at": span_time(50),
            "status": "OK",
        },
        {
            "id": "tool-1",
            "parent_span_id": "root",
            "span_type": "TOOL",
            "name": "Search",
            "started_at": span_time(5),
            "ended_at": span_time(10),
            "status": "ERROR",
            "error": "service unavailable",
            "attributes": {"retry_count": 2},
        },
        {
            "id": "tool-2",
            "parent_span_id": "root",
            "span_type": "TOOL",
            "name": "Search",
            "started_at": span_time(11),
            "ended_at": span_time(15),
            "status": "ERROR",
            "error": "service unavailable",
            "events": [{"name": "tool.retry", "timestamp": span_time(12), "attributes": {}}],
        },
        {
            "id": "plan-1",
            "parent_span_id": "root",
            "span_type": "PLANNER",
            "name": "Planner",
            "started_at": span_time(16),
            "ended_at": span_time(20),
            "status": "OK",
            "attributes": {"plan_id": "plan-cycle-1"},
        },
        {
            "id": "plan-2",
            "parent_span_id": "root",
            "span_type": "PLANNER",
            "name": "Planner",
            "started_at": span_time(21),
            "ended_at": span_time(25),
            "status": "OK",
            "attributes": {"plan_id": "plan-cycle-1"},
        },
        {
            "id": "handoff",
            "parent_span_id": "root",
            "span_type": "AGENT",
            "name": "Verifier",
            "started_at": span_time(26),
            "ended_at": span_time(35),
            "status": "OK",
            "attributes": {"parent_agent": "ResearchAgent", "handoff_to": "Verifier"},
        },
    ]
    if run_id != "analytics-run":
        id_map = {span["id"]: f"{run_id}:{span['id']}" for span in spans}
        for span in spans:
            span["id"] = id_map[span["id"]]
            if span.get("parent_span_id"):
                span["parent_span_id"] = id_map[span["parent_span_id"]]
    return {
        "run": {
            "run_id": run_id,
            "project_id": project,
            "application_id": f"{project}-app",
            "environment": "test",
            "agent_name": "ResearchAgent",
            "agent_version": version,
            "trace_id": f"trace-{run_id}",
            "status": "SUCCEEDED",
            "started_at": span_time(0),
            "completed_at": span_time(50),
        },
        "root_span_id": "root" if run_id == "analytics-run" else f"{run_id}:root",
        "spans": spans,
    }


def test_run_analysis_persists_explicit_retry_handoff_and_loop_evidence(client):
    original = telemetry()
    assert client.post("/v1/traces", json=original).status_code == 202
    result = client.post(
        "/runs/analytics-run/agent-analytics",
        json={
            "config": {
                "repeated_tool_call_threshold": 2,
                "repeated_failure_threshold": 2,
                "repeated_plan_threshold": 2,
                "excessive_step_count_threshold": 4,
                "repeated_tool_call_severity": "INFO",
            }
        },
    )
    assert result.status_code == 201, result.text
    body = result.json()
    analysis = body["analysis"]
    assert analysis["retry_analysis"]["status"] == "AVAILABLE"
    assert analysis["retry_analysis"]["count"] == 3
    assert analysis["handoffs"]["relationships"][0]["from_agent"] == "ResearchAgent"
    assert analysis["tool_usage"] == [
        {
            "tool_name": "Search",
            "calls": 2,
            "succeeded": 0,
            "failed": 2,
            "outcome_unavailable": 0,
            "span_ids": ["tool-1", "tool-2"],
        }
    ]
    patterns = {event["pattern"]: event for event in analysis["loop_detection"]["events"]}
    assert set(patterns) == {
        "REPEATED_TOOL_CALL",
        "REPEATED_FAILURE",
        "REPEATED_PLAN",
        "EXCESSIVE_STEP_COUNT",
    }
    assert patterns["REPEATED_TOOL_CALL"]["detection_method"] == "CONSECUTIVE_TOOL_NAME_HEURISTIC"
    assert patterns["REPEATED_TOOL_CALL"]["severity"] == "INFO"
    assert patterns["REPEATED_PLAN"]["detection_method"] == "EXPLICIT_PLAN_ID"
    assert set(patterns["REPEATED_FAILURE"]["span_ids"]) == {"tool-1", "tool-2"}
    stored = client.get("/runs/analytics-run/agent-analytics")
    assert stored.status_code == 200
    assert stored.json()["id"] == body["id"]
    # Analytics are separate derived records; source telemetry remains unchanged.
    trace = client.get("/runs/analytics-run").json()
    assert len(trace["spans"]) == len(original["spans"])
    assert trace["spans"][1]["attributes"]["retry_count"] == 2

    below_threshold = client.post(
        "/runs/analytics-run/agent-analytics",
        json={
            "config": {
                "repeated_tool_call_threshold": 3,
                "repeated_failure_threshold": 3,
                "repeated_plan_threshold": 3,
                "excessive_step_count_threshold": 6,
            }
        },
    )
    assert below_threshold.status_code == 201
    assert below_threshold.json()["analysis"]["loop_detection"]["events"] == []


def test_missing_retry_and_handoff_telemetry_is_unavailable(client):
    payload = telemetry("no-explicit-data")
    for span in payload["spans"]:
        span.pop("attributes", None)
        span.pop("events", None)
    assert client.post("/v1/traces", json=payload).status_code == 202
    result = client.post("/runs/no-explicit-data/agent-analytics", json={})
    assert result.status_code == 201
    analysis = result.json()["analysis"]
    assert analysis["retry_analysis"] == {"status": "UNAVAILABLE", "count": None, "evidence": []}
    assert analysis["handoffs"] == {"status": "UNAVAILABLE", "relationships": []}


def test_aggregate_filters_project_application_agent_version_and_time(client):
    assert client.post("/v1/traces", json=telemetry()).status_code == 202
    no_tools = telemetry("analytics-no-tools")
    no_tools["spans"] = [no_tools["spans"][0]]
    assert client.post("/v1/traces", json=no_tools).status_code == 202
    second = telemetry("other-project-run", project="other-project", version="v1")
    assert client.post("/v1/traces", json=second).status_code == 202
    after = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    response = client.get(
        "/agent-analytics",
        params={
            "project_id": "analytics-project",
            "application_id": "analytics-project-app",
            "agent_name": "ResearchAgent",
            "agent_version": "v9",
            "started_after": after,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_count"] == 2
    assert len(body["groups"]) == 1
    group = body["groups"][0]
    assert group["agent_version"] == "v9"
    assert group["tool_usage"][0]["calls"] == 2
    assert group["tool_usage_status"] == "PARTIAL"
    assert group["retry_status"] == "PARTIAL"
    assert group["retry_count"] is None
    assert group["handoff_status"] == "PARTIAL"
    assert group["handoff_count"] is None
    assert (
        client.get("/agent-analytics", params={"project_id": "other-project"}).json()["run_count"]
        == 1
    )


def test_run_analysis_config_validation_and_missing_run(client):
    assert client.post("/runs/missing/agent-analytics", json={}).status_code == 404
    assert client.post("/v1/traces", json=telemetry()).status_code == 202
    invalid = client.post(
        "/runs/analytics-run/agent-analytics",
        json={
            "config": {"repeated_tool_call_threshold": 1},
        },
    )
    assert invalid.status_code == 422
