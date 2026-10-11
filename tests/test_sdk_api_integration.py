from conftest import SyncASGITransport

import tracecore
from tracecore.sdk import TraceCoreClient


def test_sdk_exports_real_trace_through_existing_ingestion_api(client) -> None:
    sdk = TraceCoreClient(
        "http://test",
        project_id="sdk-e2e-project",
        application_id="sdk-e2e-app",
        environment="test",
        transport=SyncASGITransport(),
    )
    with (
        sdk,
        sdk.run(
            agent_name="SDKIntegrationAgent",
            agent_version="test-1",
            input={"task": "persist this execution", "api_key": "sk-test-secret-value-1234567890"},
            run_id="sdk-e2e-run",
            trace_id="sdk-e2e-trace",
        ) as run,
    ):
        with tracecore.span("Plan", span_type="PLANNER") as planner:
            planner_id = planner.span_id
        run.finish("SUCCEEDED", output={"result": "observed"})

    response = client.get("/traces/sdk-e2e-trace")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == "sdk-e2e-run"
    assert body["run"]["trace_id"] == "sdk-e2e-trace"
    assert body["run"]["input"]["api_key"] == "[REDACTED]"
    assert body["trace"]["root_span_id"] == body["spans"][0]["id"]
    planner = next(span for span in body["spans"] if span["id"] == planner_id)
    assert planner["parent_span_id"] == body["trace"]["root_span_id"]
