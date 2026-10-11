from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def payload(
    run_id: str,
    *,
    status: str = "FAILED",
    cost: float | None = None,
    project_id: str = "mon-project",
) -> dict:
    now = datetime.now(UTC)
    metadata = {"cost_verified": True, "verified_cost": cost} if cost is not None else {}
    return {
        "run": {
            "run_id": run_id,
            "project_id": project_id,
            "application_id": f"app-{project_id}",
            "environment": "production",
            "agent_name": "worker",
            "agent_version": "v3",
            "trace_id": f"trace-{run_id}",
            "status": status,
            "started_at": now.isoformat(),
            "completed_at": (now + timedelta(milliseconds=23)).isoformat(),
            "metadata": metadata,
            "token_usage": {"total_tokens": 11},
        },
        "root_span_id": f"root-{run_id}",
        "spans": [
            {
                "id": f"root-{run_id}",
                "span_type": "AGENT",
                "name": "agent",
                "started_at": now.isoformat(),
                "ended_at": (now + timedelta(milliseconds=23)).isoformat(),
                "status": "ERROR" if status == "FAILED" else "OK",
            },
            {
                "id": f"llm-{run_id}",
                "parent_span_id": f"root-{run_id}",
                "span_type": "LLM",
                "name": "model",
                "started_at": now.isoformat(),
                "ended_at": (now + timedelta(milliseconds=10)).isoformat(),
                "status": "ERROR",
                "attributes": {"total_tokens": 11},
            },
            {
                "id": f"tool-{run_id}",
                "parent_span_id": f"root-{run_id}",
                "span_type": "TOOL",
                "name": "search",
                "started_at": now.isoformat(),
                "ended_at": (now + timedelta(milliseconds=5)).isoformat(),
                "status": "ERROR",
                "attributes": {"retry_count": 2},
            },
        ],
    }


def test_metrics_use_stored_runs_and_report_unavailable_data(client):
    trace_payload = payload("one", cost=0.42)
    trace_payload["spans"][2]["events"] = [
        {"name": "retry", "timestamp": trace_payload["spans"][2]["started_at"]}
    ]
    assert client.post("/v1/traces", json=trace_payload).status_code == 202
    assert (
        client.post(
            "/v1/traces", json=payload("other-project", project_id="different-project")
        ).status_code
        == 202
    )
    response = client.get(
        "/monitoring/metrics", params={"project_id": "mon-project", "minimum_samples": 1}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_count"] == 1
    assert body["metrics"]["failure_count"]["value"] == 1
    assert body["metrics"]["tool_failure_count"]["value"] == 1
    assert body["metrics"]["model_failure_count"]["value"] == 1
    assert body["metrics"]["retry_count"]["value"] == 2
    assert body["metrics"]["verified_cost"]["value"] == 0.42
    assert body["metrics"]["latency_ms"]["p95"] is not None
    assert (
        client.get("/monitoring/metrics", params={"project_id": "different-project"}).json()[
            "metrics"
        ]["latency_ms"]["status"]
        == "UNAVAILABLE"
    )
    assert (
        client.get("/monitoring/metrics", params={"project_id": "different-project"}).json()[
            "metrics"
        ]["verified_cost"]["status"]
        == "UNAVAILABLE"
    )


def test_budget_status_and_alert_cooldown_resolution(client):
    assert client.post("/v1/traces", json=payload("failure")).status_code == 202
    budget = client.post(
        "/monitoring/budgets",
        json={
            "project_id": "mon-project",
            "name": "small",
            "limits": {"tool_calls": 0, "verified_cost": 1},
        },
    )
    assert budget.status_code == 201
    budget_status = client.get("/runs/failure/budgets").json()["budgets"][0]["usage"]
    assert budget_status["tool_calls"]["exceeded"] is True
    assert budget_status["verified_cost"]["status"] == "UNAVAILABLE"
    rule = client.post(
        "/monitoring/alert-rules",
        json={
            "project_id": "mon-project",
            "name": "failures",
            "metric": "failure_rate",
            "operator": "gt",
            "threshold": 0,
            "window_minutes": 60,
            "minimum_samples": 1,
            "severity": "ERROR",
            "destination": {"type": "database"},
            "cooldown_seconds": 3600,
        },
    )
    assert rule.status_code == 201
    first = client.post(f"/monitoring/alert-rules/{rule.json()['id']}/evaluate").json()[
        "occurrences"
    ]
    second = client.post(f"/monitoring/alert-rules/{rule.json()['id']}/evaluate").json()[
        "occurrences"
    ]
    assert len(first) == len(second) == 1
    assert first[0]["suppressed"] is False
    assert second[0]["suppressed"] is True
    assert client.patch(f"/monitoring/alerts/{first[0]['id']}").json()["status"] == "RESOLVED"
    assert (
        len(
            client.get("/monitoring/alerts", params={"project_id": "mon-project"}).json()[
                "occurrences"
            ]
        )
        == 2
    )


def test_webhook_delivery_records_safe_attempt(client):
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                {
                    "body": self.rfile.read(int(self.headers["Content-Length"])),
                    "key": self.headers.get("Idempotency-Key"),
                }
            )
            self.send_response(500 if len(received) == 1 else 204)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert client.post("/v1/traces", json=payload("webhook-run")).status_code == 202
        rule = client.post(
            "/monitoring/alert-rules",
            json={
                "project_id": "mon-project",
                "name": "webhook failures",
                "metric": "failure_rate",
                "operator": "gt",
                "threshold": 0,
                "window_minutes": 60,
                "minimum_samples": 1,
                "severity": "ERROR",
                "destination": {
                    "type": "webhook",
                    "url": f"http://127.0.0.1:{server.server_port}/alerts",
                },
            },
        )
        assert rule.status_code == 201
        occurrence = client.post(f"/monitoring/alert-rules/{rule.json()['id']}/evaluate").json()[
            "occurrences"
        ][0]
        assert len(received) == 2 and received[0]["key"] == occurrence["fingerprint"]
        assert [item["status"] for item in occurrence["deliveries"]] == [
            "FAILED",
            "DELIVERED",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_alert_does_not_fire_below_sample_minimum(client):
    assert client.post("/v1/traces", json=payload("single-failure")).status_code == 202
    rule = client.post(
        "/monitoring/alert-rules",
        json={
            "project_id": "mon-project",
            "name": "sample guarded",
            "metric": "failure_rate",
            "operator": "gt",
            "threshold": 0,
            "window_minutes": 60,
            "minimum_samples": 2,
            "severity": "WARN",
            "destination": {"type": "database"},
        },
    )
    assert rule.status_code == 201
    result = client.post(f"/monitoring/alert-rules/{rule.json()['id']}/evaluate")
    assert result.json()["occurrences"] == []


def test_nearest_rank_percentile():
    from tracecore.monitoring import MonitoringService

    assert MonitoringService._percentile([100, 1, 50, 10], 0.50) == 10
    assert MonitoringService._percentile([100, 1, 50, 10], 0.95) == 100
