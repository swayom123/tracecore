from datetime import UTC, datetime, timedelta


def stored_run(run_id: str = "gov-run", project: str = "gov-project") -> dict:
    start = datetime.now(UTC)
    end = start + timedelta(milliseconds=22)

    def at(offset: int) -> str:
        return (start + timedelta(milliseconds=offset)).isoformat()

    return {
        "run": {
            "run_id": run_id,
            "project_id": project,
            "application_id": f"{project}-app",
            "environment": "production",
            "agent_name": "Researcher",
            "agent_version": None,
            "trace_id": f"trace-{run_id}",
            "status": "SUCCEEDED",
            "started_at": at(0),
            "completed_at": end.isoformat(),
            "metadata": {"logging_mode": "REDACTED"},
        },
        "root_span_id": "root-" + run_id,
        "spans": [
            {
                "id": "root-" + run_id,
                "span_type": "AGENT",
                "name": "Researcher",
                "started_at": at(0),
                "ended_at": at(22),
                "status": "OK",
            },
            {
                "id": "llm-" + run_id,
                "parent_span_id": "root-" + run_id,
                "span_type": "LLM",
                "name": "chat",
                "started_at": at(1),
                "ended_at": at(10),
                "status": "OK",
                "attributes": {"model": "model-z", "prompt_version": "prompt-4"},
            },
            {
                "id": "tool-" + run_id,
                "parent_span_id": "root-" + run_id,
                "span_type": "TOOL",
                "name": "Shell",
                "started_at": at(11),
                "ended_at": at(14),
                "status": "OK",
            },
        ],
    }


def policy(project: str, name: str, conditions: list[dict], effect: str = "BLOCK") -> dict:
    return {"project_id": project, "name": name, "conditions": conditions, "effect": effect}


def test_policy_versions_evidence_and_violation_waiver_history(client):
    assert client.post("/v1/traces", json=stored_run()).status_code == 202
    first = client.post(
        "/policies",
        json=policy(
            "gov-project",
            "prod-controls",
            [
                {"field": "environment", "operator": "eq", "value": "production"},
                {"field": "agent_version", "operator": "not_exists"},
                {"field": "model", "operator": "not_in", "value": ["approved-model"]},
                {"field": "tool_name", "operator": "not_in", "value": ["Search", "Retrieval"]},
            ],
        ),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/policies",
        json=policy(
            "gov-project",
            "prod-controls",
            [
                {"field": "environment", "operator": "eq", "value": "production"},
            ],
            "WARN",
        ),
    )
    assert second.status_code == 201
    assert first.json()["version"] == 1 and second.json()["version"] == 2
    checked = client.post("/runs/gov-run/policy-check", json={"policy_ids": [first.json()["id"]]})
    assert checked.status_code == 201, checked.text
    evaluation = checked.json()["evaluations"][0]
    assert evaluation["result"] == "MATCH"
    assert evaluation["decision"] == "BLOCK"
    assert evaluation["policy_version"] == 1
    assert evaluation["policy_snapshot"]["version"] == 1
    violation = evaluation["violation"]
    assert violation["status"] == "OPEN"
    assert {item["field"] for item in evaluation["evidence"]} == {
        "environment",
        "agent_version",
        "model",
        "tool_name",
    }
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    transition = client.patch(
        f"/violations/{violation['id']}",
        json={
            "status": "WAIVED",
            "actor": "reviewer@example.test",
            "reason": "Accepted exception",
            "expires_at": future,
        },
    )
    assert transition.status_code == 200, transition.text
    assert transition.json()["waiver"]["actor"] == "reviewer@example.test"
    persisted = client.get(f"/violations/{violation['id']}")
    assert persisted.status_code == 200
    assert persisted.json()["waiver"]["expired"] is False
    history = client.get(f"/violations/{violation['id']}/history").json()["history"]
    assert [item["to_status"] for item in history] == ["OPEN", "WAIVED"]
    assert history[-1]["reason"] == "Accepted exception"
    assert (
        client.post("/v1/traces", json=stored_run("other-run", "other-project")).status_code == 202
    )
    assert (
        client.get("/violations", params={"project_id": "other-project"}).json()["violations"] == []
    )


def test_unknown_telemetry_is_unavailable_and_policy_schema_is_strict(client):
    payload = stored_run("gov-missing")
    payload["run"].pop("metadata")
    payload["spans"][1]["attributes"].pop("model")
    payload["spans"][1]["attributes"].pop("prompt_version")
    assert client.post("/v1/traces", json=payload).status_code == 202
    response = client.post(
        "/policies",
        json=policy(
            "gov-project",
            "requires-model",
            [
                {"field": "model", "operator": "in", "value": ["approved-model"]},
                {"field": "logging_mode", "operator": "eq", "value": "METADATA_ONLY"},
            ],
            "WARN",
        ),
    )
    assert response.status_code == 201
    evaluation = client.post("/runs/gov-missing/policy-check", json={}).json()["evaluations"][0]
    assert evaluation["result"] == "UNAVAILABLE"
    assert all(item["status"] == "UNAVAILABLE" for item in evaluation["evidence"])
    assert evaluation["violation"] is None
    unsupported = client.post(
        "/policies",
        json=policy(
            "gov-project",
            "invalid",
            [
                {"field": "secret_prompt", "operator": "eq", "value": "x"},
            ],
        ),
    )
    assert unsupported.status_code == 422
    bad_operator = client.post(
        "/policies",
        json=policy(
            "gov-project",
            "invalid-op",
            [
                {"field": "environment", "operator": "contains", "value": "prod"},
            ],
        ),
    )
    assert bad_operator.status_code == 422


def test_policy_evaluation_is_project_scoped_and_waiver_requires_metadata(client):
    assert client.post("/v1/traces", json=stored_run()).status_code == 202
    foreign = client.post(
        "/policies",
        json=policy(
            "other-project",
            "foreign",
            [
                {"field": "environment", "operator": "eq", "value": "production"},
            ],
        ),
    )
    assert foreign.status_code == 404
    local = client.post(
        "/policies",
        json=policy(
            "gov-project",
            "local",
            [
                {"field": "tool_name", "operator": "in", "value": ["Shell"]},
            ],
        ),
    )
    cross = client.post("/runs/gov-run/policy-check", json={"policy_ids": ["not-local"]})
    assert cross.status_code == 422
    assert (
        client.post(
            "/runs/gov-run/policy-check", json={"policy_ids": [local.json()["id"]]}
        ).status_code
        == 201
    )
    violations = client.get("/violations", params={"project_id": "gov-project"}).json()[
        "violations"
    ]
    assert len(violations) == 1
    invalid_waiver = client.patch(
        f"/violations/{violations[0]['id']}",
        json={
            "status": "WAIVED",
            "actor": "reviewer",
            "reason": "test",
        },
    )
    assert invalid_waiver.status_code == 422
    assert (
        client.patch(
            f"/violations/{violations[0]['id']}",
            json={
                "status": "ACKNOWLEDGED",
                "actor": "reviewer",
                "reason": "triaged",
            },
        ).status_code
        == 200
    )


def test_allow_and_require_review_effects_are_reported_without_fake_compliance(client):
    assert client.post("/v1/traces", json=stored_run()).status_code == 202
    matching = [{"field": "run_status", "operator": "eq", "value": "SUCCEEDED"}]
    allowed = client.post(
        "/policies", json=policy("gov-project", "allow-recorded", matching, "ALLOW")
    )
    review = client.post(
        "/policies", json=policy("gov-project", "review-recorded", matching, "REQUIRE_REVIEW")
    )
    assert allowed.status_code == review.status_code == 201
    response = client.post("/runs/gov-run/policy-check", json={})
    assert response.status_code == 201
    results = {item["policy_snapshot"]["name"]: item for item in response.json()["evaluations"]}
    assert results["allow-recorded"]["decision"] == "ALLOW"
    assert results["allow-recorded"]["violation"] is None
    assert results["review-recorded"]["decision"] == "REQUIRE_REVIEW"
    assert results["review-recorded"]["violation"]["review_required"] is True
