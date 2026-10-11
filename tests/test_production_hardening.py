import json
from datetime import UTC, datetime, timedelta

from tracecore import api as api_module
from tracecore.config import Settings
from tracecore.models import (
    ApiCredential,
    AuditEvent,
    EvaluationDataset,
    Experiment,
    ExperimentRun,
    RateLimitBucket,
)
from tracecore.redaction import BasicRedactionService
from tracecore.retention import retention_days_by_environment


def payload(run_id: str, project_id: str, started_at: datetime) -> dict:
    ended_at = started_at + timedelta(milliseconds=25)
    return {
        "run": {
            "run_id": run_id,
            "project_id": project_id,
            "application_id": f"app-{project_id}",
            "environment": "test",
            "agent_name": "security-test-agent",
            "agent_version": "v1",
            "trace_id": f"trace-{run_id}",
            "status": "SUCCEEDED",
            "started_at": started_at.isoformat(),
            "completed_at": ended_at.isoformat(),
        },
        "root_span_id": f"span-{run_id}",
        "spans": [
            {
                "id": f"span-{run_id}",
                "span_type": "AGENT",
                "name": "security-test-agent",
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "status": "OK",
            }
        ],
    }


def test_production_auth_fails_closed_and_roles_are_enforced(client, monkeypatch):
    monkeypatch.setattr(
        api_module, "get_settings", lambda: Settings(runtime_environment="production")
    )
    assert client.get("/runs").status_code == 503

    token = "v" * 40
    settings = Settings(
        runtime_environment="production",
        auth_mode="bearer",
        api_tokens=json.dumps(
            {
                token: {
                    "subject": "read-only",
                    "role": "VIEWER",
                    "organization_id": "org-one",
                    "project_ids": ["project-one"],
                    "environments": ["test"],
                }
            }
        ),
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    assert client.get("/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert (
        client.post("/v1/traces", json={}, headers={"Authorization": f"Bearer {token}"}).status_code
        == 403
    )


def test_project_and_organization_scope_blocks_cross_org_reads(client, monkeypatch):
    token_a = "a" * 40
    settings = Settings(
        runtime_environment="production",
        auth_mode="bearer",
        api_tokens=json.dumps(
            {
                token_a: {
                    "subject": "writer",
                    "role": "AI_ENGINEER",
                    "organization_id": "org-one",
                    "project_ids": ["shared-project-id"],
                    "environments": ["test"],
                }
            }
        ),
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    response = client.post(
        "/v1/traces",
        json=payload("scoped-run", "shared-project-id", datetime.now(UTC)),
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 202
    outside_project = client.post(
        "/v1/traces",
        json=payload("outside-run", "other-project", datetime.now(UTC)),
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert outside_project.status_code == 403
    token_staging = "c" * 40
    settings.api_tokens = json.dumps(
        {
            token_staging: {
                "subject": "staging-viewer",
                "role": "VIEWER",
                "organization_id": "org-one",
                "project_ids": ["shared-project-id"],
                "environments": ["staging"],
            }
        }
    )
    scoped_runs = client.get("/runs", headers={"Authorization": f"Bearer {token_staging}"})
    assert scoped_runs.status_code == 200
    assert scoped_runs.json()["total"] == 0
    token_admin = "d" * 40
    settings.api_tokens = json.dumps(
        {
            token_admin: {
                "subject": "test-admin",
                "role": "ADMIN",
                "organization_id": "org-one",
                "project_ids": ["shared-project-id"],
                "environments": ["test"],
            }
        }
    )
    assert (
        client.get(
            "/runs/scoped-run", headers={"Authorization": f"Bearer {token_admin}"}
        ).status_code
        == 200
    )
    audit_events = client.get(
        "/audit/events", headers={"Authorization": f"Bearer {token_admin}"}
    ).json()["events"]
    assert audit_events
    assert all(event["details"]["environment"] == "test" for event in audit_events)
    token_b = "b" * 40
    settings.api_tokens = json.dumps(
        {
            token_b: {
                "subject": "other-org",
                "role": "VIEWER",
                "organization_id": "org-two",
                "project_ids": ["shared-project-id"],
                "environments": ["test"],
            }
        }
    )
    response = client.get("/runs/scoped-run", headers={"Authorization": f"Bearer {token_b}"})
    assert response.status_code == 404


def test_write_payload_limit_and_rate_limit(client, monkeypatch):
    limited = Settings(max_payload_bytes=1024, rate_limit_per_minute=600)
    monkeypatch.setattr(api_module, "get_settings", lambda: limited)
    oversized = client.post("/v1/traces", content=b"{" + b" " * 2048 + b"}")
    assert oversized.status_code == 413

    limited.rate_limit_per_minute = 1
    api_module.rate_limiter._requests.clear()
    cleanup = api_module.SessionLocal()
    cleanup.query(RateLimitBucket).delete()
    cleanup.commit()
    cleanup.close()
    assert client.get("/runs").status_code == 200
    assert client.get("/runs").status_code == 429


def test_database_credentials_are_hashed_scoped_and_revocable(client, monkeypatch):
    bootstrap = "bootstrap-token-" + "x" * 40
    settings = Settings(
        runtime_environment="production",
        auth_mode="bearer",
        api_tokens=json.dumps(
            {
                bootstrap: {
                    "subject": "platform-admin",
                    "role": "ADMIN",
                    "organization_id": "org-credentials",
                    "project_ids": ["credential-project"],
                    "environments": ["test"],
                }
            }
        ),
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    headers = {"Authorization": f"Bearer {bootstrap}"}
    created = client.post(
        "/auth/tokens",
        headers=headers,
        json={
            "name": "ci-reader",
            "subject": "ci",
            "role": "VIEWER",
            "organization_id": "org-credentials",
            "project_ids": ["credential-project"],
            "environments": ["test"],
        },
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    token_id = created.json()["id"]
    assert token.startswith("tc_")
    session = api_module.SessionLocal()
    try:
        record = session.get(ApiCredential, token_id)
        assert record is not None
        assert record.token_hash != token
        assert token not in record.token_hash
    finally:
        session.close()
    assert client.get("/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    out_of_scope = client.post(
        "/auth/tokens",
        headers=headers,
        json={
            "name": "cross-scope",
            "subject": "bad",
            "role": "VIEWER",
            "organization_id": "org-credentials",
            "project_ids": ["other-project"],
            "environments": ["test"],
        },
    )
    assert out_of_scope.status_code == 403
    session = api_module.SessionLocal()
    try:
        record = session.get(ApiCredential, token_id)
        assert record is not None
        record.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        session.commit()
    finally:
        session.close()
    assert client.get("/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    session = api_module.SessionLocal()
    try:
        record = session.get(ApiCredential, token_id)
        assert record is not None
        record.expires_at = None
        session.commit()
    finally:
        session.close()
    revoked = client.request("DELETE", f"/auth/tokens/{token_id}", headers=headers)
    assert revoked.status_code == 200
    assert client.get("/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    listed = client.get("/auth/tokens", headers=headers)
    assert listed.status_code == 200
    assert token not in listed.text
    actions = [
        event["action"] for event in client.get("/audit/events", headers=headers).json()["events"]
    ]
    assert "auth.credential.create" in actions, actions
    assert "auth.credential.revoke" in actions, actions


def test_rate_limit_is_shared_between_limiter_instances(client):
    factory = api_module.SessionLocal
    first = api_module.RequestRateLimiter()
    second = api_module.RequestRateLimiter()
    assert first.allow_shared("shared-subject", 1, factory, now=1200)
    assert not second.allow_shared("shared-subject", 1, factory, now=1201)
    assert second.allow_shared("shared-subject", 1, factory, now=1260)


def test_validation_error_does_not_echo_submitted_values(client):
    response = client.post("/v1/traces", json={"secret_marker": "do-not-echo-this"})
    assert response.status_code == 422
    assert "do-not-echo-this" not in response.text


def test_unauthenticated_requests_are_rate_limited(client, monkeypatch):
    api_module.rate_limiter._requests.clear()
    settings = Settings(
        runtime_environment="production", auth_mode="bearer", rate_limit_per_minute=1
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    assert client.get("/runs").status_code == 503
    assert client.get("/runs").status_code == 429


def test_redaction_catches_credentials_and_custom_patterns():
    redactor = BasicRedactionService(r'["CUSTOM-[A-Z0-9]+"]')
    result = redactor.redact(
        {
            "authorization": "Bearer should-not-remain",
            "customer_record": {"name": "not-retained"},
            "message": "token=secret-value AKIA1234567890ABCDEF github ghp_abcdefghijklmnopqrstuvwxyz123456 custom=CUSTOM-ABC123",
        }
    )
    assert result["authorization"] == "[REDACTED]"
    assert result["customer_record"] == "[REDACTED]"
    assert "secret-value" not in result["message"]
    assert "AKIA1234567890ABCDEF" not in result["message"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in result["message"]
    assert "CUSTOM-ABC123" not in result["message"]
    assert retention_days_by_environment('{"prod": 90}') == {"prod": 90}


def test_retention_deletion_is_idempotent_audited_and_chain_verifiable(client):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    assert (
        client.post("/v1/traces", json=payload("old-run", "retention-project", old)).status_code
        == 202
    )
    cutoff = datetime(2021, 1, 1, tzinfo=UTC).isoformat()
    first = client.request(
        "DELETE",
        "/projects/retention-project/runs",
        params={"environment": "test", "before": cutoff},
    )
    assert first.status_code == 200
    assert first.json()["deleted_runs"] == 1
    again = client.request(
        "DELETE",
        "/projects/retention-project/runs",
        params={"environment": "test", "before": cutoff},
    )
    assert again.status_code == 200
    assert again.json()["deleted_runs"] == 0
    assert client.get("/runs/old-run").status_code == 404
    events = client.get("/audit/events").json()["events"]
    assert any(event["action"] == "retention.delete_runs" for event in events)
    verification = client.get("/audit/verify").json()
    assert verification["valid"] is True, (verification, events)


def test_retention_removes_run_mappings_and_comparison_snapshots(client):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    assert (
        client.post(
            "/v1/traces", json=payload("experiment-run-source", "experiment-retention-project", old)
        ).status_code
        == 202
    )
    session = api_module.SessionLocal()
    dataset = EvaluationDataset(
        project_id="experiment-retention-project", name="retention-dataset", version=1
    )
    session.add(dataset)
    session.flush()
    experiment = Experiment(
        project_id="experiment-retention-project",
        name="retention-experiment",
        version=1,
        dataset_id=dataset.id,
        dataset_version=1,
        baseline_candidate_id="baseline",
        evaluator_snapshot=[],
        candidates=[{"id": "baseline", "case_runs": {"case-1": "experiment-run-source"}}],
        criteria=[],
    )
    session.add(experiment)
    session.flush()
    comparison = ExperimentRun(
        experiment_id=experiment.id,
        experiment_version=1,
        dataset_id=dataset.id,
        dataset_version=1,
        evaluator_snapshot=[],
        candidate_snapshot=experiment.candidates,
        criteria_snapshot=[],
        status="WARN",
        comparison={},
    )
    session.add(comparison)
    session.commit()
    experiment_id, comparison_id = experiment.id, comparison.id
    session.close()

    cutoff = datetime(2021, 1, 1, tzinfo=UTC).isoformat()
    response = client.request(
        "DELETE",
        "/projects/experiment-retention-project/runs",
        params={"environment": "test", "before": cutoff},
    )
    assert response.status_code == 200
    session = api_module.SessionLocal()
    try:
        stored_experiment = session.get(Experiment, experiment_id)
        assert stored_experiment is not None
        assert stored_experiment.candidates[0]["case_runs"] == {}
        assert session.get(ExperimentRun, comparison_id) is None
    finally:
        session.close()


def test_run_content_reads_are_audited(client):
    assert (
        client.post(
            "/v1/traces", json=payload("audited-run", "audit-project", datetime.now(UTC))
        ).status_code
        == 202
    )
    assert client.get("/runs/audited-run").status_code == 200
    events = client.get("/audit/events").json()["events"]
    assert any(
        event["action"] == "trace.content_access" and event["target_id"] == "audited-run"
        for event in events
    )


def test_audit_verification_detects_modified_event_content(client):
    assert (
        client.post(
            "/v1/traces", json=payload("tamper-run", "tamper-project", datetime.now(UTC))
        ).status_code
        == 202
    )
    assert client.get("/runs/tamper-run").status_code == 200
    session = api_module.SessionLocal()
    try:
        event = session.query(AuditEvent).first()
        assert event is not None
        event.details = {**event.details, "tampered": True}
        session.commit()
    finally:
        session.close()
    result = client.get("/audit/verify").json()
    assert result["valid"] is False
    assert result["event_id"]
