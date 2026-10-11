from datetime import UTC, datetime, timedelta


def make_payload(run_id: str, trace_id: str, version: str, started_at: datetime) -> dict:
    ended_at = started_at + timedelta(milliseconds=15)
    return {
        "run": {
            "run_id": run_id,
            "project_id": "project-filter",
            "application_id": "app-filter",
            "environment": "test",
            "agent_name": "filter-agent",
            "agent_version": version,
            "trace_id": trace_id,
            "status": "SUCCEEDED",
            "started_at": started_at.isoformat(),
            "completed_at": ended_at.isoformat(),
        },
        "root_span_id": f"root-{run_id}",
        "spans": [
            {
                "id": f"root-{run_id}",
                "span_type": "AGENT",
                "name": "filter-agent",
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "status": "OK",
            }
        ],
    }


def test_runs_filters_version_time_and_total(client) -> None:
    now = datetime.now(UTC)
    client.post(
        "/v1/traces", json=make_payload("old-run", "old-trace", "v1", now - timedelta(days=2))
    )
    client.post("/v1/traces", json=make_payload("recent-run", "recent-trace", "v2", now))

    response = client.get(
        "/runs",
        params={
            "agent_version": "v2",
            "started_after": (now - timedelta(days=1)).isoformat(),
            "limit": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [run["id"] for run in body["runs"]] == ["recent-run"]
    assert body["runs"][0]["agent_version"] == "v2"

    empty_page = client.get("/runs", params={"agent_version": "v2", "offset": 1, "limit": 1})
    assert empty_page.json()["total"] == 1
    assert empty_page.json()["runs"] == []

    invalid_range = client.get(
        "/runs",
        params={
            "started_after": now.isoformat(),
            "started_before": (now - timedelta(days=1)).isoformat(),
        },
    )
    assert invalid_range.status_code == 422
