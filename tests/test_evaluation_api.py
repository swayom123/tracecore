from datetime import UTC, datetime, timedelta


def telemetry() -> dict:
    started = datetime.now(UTC)
    ended = started + timedelta(milliseconds=25)
    return {
        "run": {
            "run_id": "eval-run-1",
            "project_id": "eval-project",
            "project_name": "Eval Project",
            "application_id": "eval-app",
            "application_name": "Evaluation App",
            "environment": "test",
            "agent_name": "candidate",
            "agent_version": "v1",
            "trace_id": "eval-trace-1",
            "status": "SUCCEEDED",
            "started_at": started.isoformat(),
            "completed_at": ended.isoformat(),
            "output": {"answer": "verified output"},
        },
        "root_span_id": "eval-root",
        "spans": [
            {
                "id": "eval-root",
                "span_type": "AGENT",
                "name": "candidate",
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "status": "OK",
            }
        ],
    }


def test_real_ingested_run_evaluates_and_persists_immutable_versions(client) -> None:
    assert client.post("/v1/traces", json=telemetry()).status_code == 202
    dataset_response = client.post(
        "/evaluations/datasets",
        json={
            "project_id": "eval-project",
            "name": "answer-check",
            "cases": [
                {
                    "id": "case-1",
                    "input": {"question": "test"},
                    "expected_output": {"answer": "verified output"},
                },
                {"id": "case-2", "expected_output": {"answer": "different"}},
            ],
        },
    )
    assert dataset_response.status_code == 201
    dataset = dataset_response.json()
    assert dataset["version"] == 1

    evaluator_response = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "exact-answer",
            "evaluator_type": "EXACT_MATCH",
            "config": {},
        },
    )
    assert evaluator_response.status_code == 201
    evaluator = evaluator_response.json()

    result = client.post(
        "/evaluations",
        json={
            "dataset_id": dataset["id"],
            "evaluator_ids": [evaluator["id"]],
            "case_runs": [
                {"case_id": "case-1", "run_id": "eval-run-1"},
                {"case_id": "case-2", "run_id": "eval-run-1"},
            ],
        },
    )
    assert result.status_code == 201, result.text
    summary = result.json()
    assert summary["status"] == "FAIL"
    assert summary["dataset_version"] == 1
    assert summary["evaluator_snapshot"][0]["version"] == 1
    assert (summary["passed"], summary["failed"], summary["unavailable"]) == (1, 1, 0)

    details = client.get(f"/evaluations/{summary['id']}").json()
    assert {item["status"] for item in details["results"]} == {"PASSED", "FAILED"}
    assert details["results"][0]["evaluator_version"] == 1
    assert details["results"][0]["dataset_version"] == 1
    assert any("does not exactly match" in item["reason"] for item in details["results"])
    assert any(
        item["case"]["expected_output"] == {"answer": "verified output"}
        for item in details["results"]
    )
    assert client.get("/evaluations").json()["total"] == 1

    next_dataset = client.post(
        "/evaluations/datasets",
        json={
            "project_id": "eval-project",
            "name": "answer-check",
            "cases": [{"expected_output": "new"}],
        },
    ).json()
    next_evaluator = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "exact-answer",
            "evaluator_type": "EXACT_MATCH",
            "config": {"output_path": "answer"},
        },
    ).json()
    assert next_dataset["version"] == 2
    assert next_evaluator["version"] == 2
    assert client.get(f"/evaluations/{summary['id']}").json()["dataset_version"] == 1


def test_evaluation_validation_and_unavailable_cost(client) -> None:
    client.post("/v1/traces", json=telemetry())
    dataset = client.post(
        "/evaluations/datasets",
        json={
            "project_id": "eval-project",
            "name": "cost",
            "cases": [{"id": "cost-case"}],
        },
    ).json()
    invalid = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "bad-regex",
            "evaluator_type": "REGEX_MATCH",
            "config": {"pattern": "["},
        },
    )
    assert invalid.status_code == 422
    evaluator = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "cost-limit",
            "evaluator_type": "COST_THRESHOLD",
            "config": {"max_cost": 0.25},
        },
    ).json()
    response = client.post(
        "/evaluations",
        json={
            "dataset_id": dataset["id"],
            "evaluator_ids": [evaluator["id"]],
            "case_runs": [{"case_id": "cost-case", "run_id": "eval-run-1"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["unavailable"] == 1
    result = client.get(f"/evaluations/{response.json()['id']}").json()["results"][0]
    assert result["status"] == "UNAVAILABLE"
    assert "Cost unavailable" in result["reason"]

    bad_mapping = client.post(
        "/evaluations",
        json={
            "dataset_id": dataset["id"],
            "evaluator_ids": [evaluator["id"]],
            "case_runs": [{"case_id": "unknown", "run_id": "eval-run-1"}],
        },
    )
    assert bad_mapping.status_code == 422


def test_metadata_only_mode_does_not_compare_omission_markers(client, monkeypatch) -> None:
    from tracecore import api
    from tracecore.config import Settings

    monkeypatch.setattr(api, "get_settings", lambda: Settings(logging_mode="METADATA_ONLY"))
    assert client.post("/v1/traces", json=telemetry()).status_code == 202
    dataset = client.post(
        "/evaluations/datasets",
        json={
            "project_id": "eval-project",
            "name": "metadata-only",
            "cases": [{"id": "metadata-case", "expected_output": {"answer": "verified output"}}],
        },
    ).json()
    evaluator = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "metadata-exact",
            "evaluator_type": "EXACT_MATCH",
        },
    ).json()
    result = client.post(
        "/evaluations",
        json={
            "dataset_id": dataset["id"],
            "evaluator_ids": [evaluator["id"]],
            "case_runs": [{"case_id": "metadata-case", "run_id": "eval-run-1"}],
        },
    )
    assert result.status_code == 201
    stored = client.get(f"/evaluations/{result.json()['id']}").json()["results"][0]
    assert stored["status"] == "UNAVAILABLE"
    assert "omitted by the logging mode" in stored["reason"]
