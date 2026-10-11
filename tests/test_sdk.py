import asyncio
import json
from datetime import datetime

import httpx
import pytest

import tracecore
from tracecore.sdk import TraceCoreClient


def make_client(handler) -> TraceCoreClient:
    return TraceCoreClient(
        "http://tracecore.test",
        project_id="sdk-project",
        application_id="sdk-app",
        environment="test",
        transport=httpx.MockTransport(handler),
    )


def test_run_and_span_export_explicit_outcome_redaction_and_parent_ids() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/traces"
        sent.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": ["stable-run"]})

    @tracecore.trace_tool(capture_input=True, capture_output=True)
    def lookup(query: str, api_key: str) -> dict[str, str]:
        return {"answer": f"found {query}"}

    with (
        make_client(handler) as client,
        client.run(
            agent_name="ResearchAgent",
            agent_version="v7",
            input={"question": "Where?", "password": "do-not-send"},
            run_id="stable-run",
            trace_id="stable-trace",
        ) as current_run,
    ):
        root_id = current_run.root_span_id
        with tracecore.span("Planner", span_type="PLANNER") as planner:
            planner.set_attribute("step", 1)
        child_id = planner.span_id
        result = lookup("site A", "sk-test-secret-value-1234567890")
        current_run.finish("SUCCEEDED", output=result)

    payload = sent[0]
    assert payload["run"]["run_id"] == "stable-run"
    assert payload["run"]["trace_id"] == "stable-trace"
    assert payload["run"]["status"] == "SUCCEEDED"
    assert payload["root_span_id"] == root_id
    assert payload["run"]["input"]["password"] == "[REDACTED]"
    assert payload["run"]["started_at"] < payload["run"]["completed_at"]
    timestamps = [datetime.fromisoformat(item["started_at"]) for item in payload["spans"]]
    assert all(timestamp.tzinfo is not None for timestamp in timestamps)
    planner_payload = next(item for item in payload["spans"] if item["id"] == child_id)
    tool_payload = next(item for item in payload["spans"] if item["span_type"] == "TOOL")
    assert planner_payload["parent_span_id"] == root_id
    assert tool_payload["parent_span_id"] == root_id
    assert tool_payload["input"]["api_key"] == "[REDACTED]"
    assert tool_payload["status"] == "OK"


def test_unfinished_clean_run_is_partial_not_success() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    with make_client(handler) as client, client.run(agent_name="UnfinishedAgent"):
        pass
    assert sent[0]["run"]["status"] == "PARTIAL"


def test_sdk_applies_configured_redaction_before_export() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    client = TraceCoreClient(
        "http://tracecore.test",
        project_id="sdk-project",
        application_id="sdk-app",
        environment="test",
        redaction_patterns=[r"CUSTOM-[A-Z0-9]{6}"],
        transport=httpx.MockTransport(handler),
    )
    with client, client.run(input={"reference": "CUSTOM-ABC123"}) as current_run:
        current_run.finish("SUCCEEDED", output={"result": "CUSTOM-DEF456"})
    assert sent[0]["run"]["input"] == {"reference": "[REDACTED]"}
    assert sent[0]["run"]["output"] == {"result": "[REDACTED]"}


def test_exception_marks_run_and_span_failed_and_is_re_raised() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    with (
        make_client(handler) as client,
        pytest.raises(RuntimeError, match="tool failed"),
        client.run(agent_name="FailureAgent"),
        tracecore.span("BrokenTool", span_type="TOOL"),
    ):
        raise RuntimeError("tool failed")
    payload = sent[0]
    assert payload["run"]["status"] == "FAILED"
    assert payload["spans"][0]["status"] == "ERROR"
    assert payload["spans"][1]["status"] == "ERROR"
    assert "tool failed" in payload["spans"][1]["error"]


def test_async_decorator_creates_llm_span() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    @tracecore.trace_llm(attributes={"llm.provider": "local"})
    async def call_model(prompt: str) -> str:
        await asyncio.sleep(0)
        return f"response to {prompt}"

    async def perform() -> str:
        return await call_model("question")

    with make_client(handler) as client, client.run(agent_name="AsyncAgent") as current_run:
        result = asyncio.run(perform())
        current_run.finish("SUCCEEDED", output=result)
    llm_span = next(item for item in sent[0]["spans"] if item["span_type"] == "LLM")
    assert llm_span["name"].endswith("call_model")
    assert llm_span["attributes"]["llm.provider"] == "local"


def test_agent_decorator_adds_agent_child_span() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    @tracecore.trace_agent
    def delegate(task: str) -> str:
        return f"delegated: {task}"

    with make_client(handler) as client, client.run(agent_name="ParentAgent") as run:
        assert delegate("summarize") == "delegated: summarize"
        run.finish("SUCCEEDED")

    child_agent = next(
        item
        for item in sent[0]["spans"]
        if item["span_type"] == "AGENT" and item["id"] != sent[0]["root_span_id"]
    )
    assert child_agent["parent_span_id"] == sent[0]["root_span_id"]
    assert child_agent["input"] is None
    assert child_agent["output"] is None


def test_export_failure_is_visible_on_clean_exit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "failed"})

    with (
        make_client(handler) as client,
        pytest.raises(tracecore.TraceExportError, match="HTTP 500"),
        client.run(agent_name="ExportFailure") as run,
    ):
        run.finish("SUCCEEDED")


def test_span_requires_active_run() -> None:
    with pytest.raises(RuntimeError, match="outside an active"), tracecore.span("orphan"):
        pass
