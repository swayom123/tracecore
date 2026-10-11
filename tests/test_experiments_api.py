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


def _ingest(client, run_id: str, output: str, *, status: str = "SUCCEEDED", cost=None):
    payload = telemetry()
    payload["run"].update(
        run_id=run_id, trace_id=f"trace-{run_id}", status=status, output=output, estimated_cost=cost
    )
    payload["root_span_id"] = f"root-{run_id}"
    payload["spans"][0].update(id=f"root-{run_id}", name="worker")
    started = datetime.now(UTC)
    ended = started + timedelta(milliseconds=40 if status == "SUCCEEDED" else 100)
    payload["run"]["started_at"] = started.isoformat()
    payload["run"]["completed_at"] = ended.isoformat()
    payload["spans"][0].update(started_at=started.isoformat(), ended_at=ended.isoformat())
    assert client.post("/v1/traces", json=payload).status_code == 202


def _definition(client):
    dataset = client.post(
        "/evaluations/datasets",
        json={
            "project_id": "eval-project",
            "name": "experiment-dataset",
            "cases": [{"id": "case-1", "expected_output": "answer"}],
        },
    ).json()
    evaluator = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "exact",
            "evaluator_type": "EXACT_MATCH",
        },
    ).json()
    return dataset, evaluator


def _request(dataset, evaluator, *, candidate_run="candidate-run"):
    return {
        "project_id": "eval-project",
        "name": "prompt-comparison",
        "dataset_id": dataset["id"],
        "evaluator_ids": [evaluator["id"]],
        "baseline_candidate_id": "baseline",
        "candidates": [
            {
                "id": "baseline",
                "name": "Prompt A",
                "configuration": {"prompt_version": "p1"},
                "case_runs": {"case-1": "baseline-run"},
            },
            {
                "id": "candidate",
                "name": "Prompt B",
                "configuration": {"prompt_version": "p2"},
                "case_runs": {"case-1": candidate_run},
            },
        ],
        "criteria": [
            {
                "metric": "evaluation_pass_rate",
                "direction": "HIGHER",
                "max_regression": 0,
                "severity": "FAIL",
            },
            {
                "metric": "latency_ms",
                "direction": "LOWER",
                "max_regression": 25,
                "severity": "WARN",
            },
            {
                "metric": "recorded_cost",
                "direction": "LOWER",
                "max_regression": 0.1,
                "severity": "FAIL",
            },
        ],
    }


def test_comparison_persists_versions_breakdown_and_explicit_status(client):
    _ingest(client, "baseline-run", "answer")
    _ingest(client, "candidate-run", "wrong", status="FAILED")
    dataset, evaluator = _definition(client)
    request = _request(dataset, evaluator)
    created = client.post("/experiments", json=request)
    assert created.status_code == 201, created.text
    experiment = created.json()
    assert experiment["version"] == 1
    assert experiment["dataset_version"] == 1
    assert experiment["evaluator_snapshot"][0]["version"] == 1
    assert experiment["baseline_candidate_id"] == "baseline"
    # A later evaluator version must not change this experiment's pinned behavior.
    later_evaluator = client.post(
        "/evaluations/evaluators",
        json={
            "project_id": "eval-project",
            "name": "exact",
            "evaluator_type": "RESPONSE_LENGTH",
            "config": {"min_chars": 1},
        },
    ).json()
    assert later_evaluator["version"] == 2

    compared = client.post(f"/experiments/{experiment['id']}/compare")
    assert compared.status_code == 201, compared.text
    summary = compared.json()
    assert summary["status"] == "FAIL"
    detail = client.get(f"/experiment-runs/{summary['id']}").json()
    assert detail["experiment_version"] == 1
    assert len(detail["results"]) == 2
    assert {result["evaluator_version"] for result in detail["results"]} == {1}
    candidate = next(
        x for x in detail["comparison"]["candidates"] if x["candidate_id"] == "candidate"
    )
    assert candidate["evaluator_breakdown"][evaluator["id"]]["FAILED"] == 1
    assert candidate["metrics"]["recorded_cost"] is None
    assert candidate["criteria"]["recorded_cost"]["status"] == "UNAVAILABLE"
    assert "unavailable" in candidate["criteria"]["recorded_cost"]["reason"].lower()
    assert detail["comparison"]["winner"] is None

    second = client.post("/experiments", json=request).json()
    assert second["version"] == 2
    assert client.get("/experiments?project_id=eval-project").json()["total"] == 2


def test_experiment_validation_rejects_unknown_case_and_invalid_criterion(client):
    _ingest(client, "baseline-run", "answer")
    _ingest(client, "candidate-run", "answer")
    dataset, evaluator = _definition(client)
    invalid_mapping = _request(dataset, evaluator, candidate_run="missing-case-run")
    invalid_mapping["candidates"][1]["case_runs"] = {"unknown-case": "candidate-run"}
    response = client.post("/experiments", json=invalid_mapping)
    assert response.status_code == 422
    invalid_direction = _request(dataset, evaluator)
    invalid_direction["criteria"][0]["direction"] = "LOWER"
    response = client.post("/experiments", json=invalid_direction)
    assert response.status_code == 422


def test_unavailable_required_metrics_warn_without_a_winner(client):
    _ingest(client, "baseline-run", "answer")
    _ingest(client, "candidate-run", "answer")
    dataset, evaluator = _definition(client)
    response = client.post("/experiments", json=_request(dataset, evaluator)).json()
    compared = client.post(f"/experiments/{response['id']}/compare").json()
    assert compared["status"] == "WARN"
    assert compared["comparison"]["winner"] is None
