# Python instrumentation SDK

The SDK is opt-in and framework independent. It creates a run root span, records child spans with context-local parent propagation, and submits one completed trace to the configured TraceCore API. It does not monkey-patch clients or collect application calls automatically.

## Configure a client

```python
import tracecore

client = tracecore.TraceCoreClient(
    "http://127.0.0.1:8000",
    project_id="billing-project",
    application_id="invoice-agent",
    environment="staging",
)
```

The endpoint is the TraceCore service base URL. `project_id`, `application_id`, and `environment` are required. The API creates project/application/environment records on first ingest. Call `client.close()` at application shutdown, or use the client as a context manager. The module-level `tracecore.init(...)` and `tracecore.run(...)` are available for applications that prefer one configured client.

For authenticated deployments, pass a bearer header from your application's secret manager, for example `headers={"Authorization": f"Bearer {os.environ['TRACECORE_API_TOKEN']}"}`. Optional client-side organization patterns can be passed as `redaction_patterns=[r"CUSTOM-[A-Z0-9]{12}"]`; configure equivalent `TRACECORE_REDACTION_PATTERNS` on the server because the server independently redacts before persistence.

## Start and finish a run explicitly

```python
with client.run(
    agent_name="InvoiceAgent",
    agent_version="v3",
    workflow_name="invoice-check",
    input={"invoice_id": "inv-204"},
) as run:
    with tracecore.span("ValidateInvoice", span_type="CUSTOM") as current_span:
        current_span.set_attribute("validation.schema", "invoice-v2")
        result = {"valid": True}
        current_span.set_output(result)
    run.finish("SUCCEEDED", output=result)
```

The SDK generates stable `run_id` and `trace_id` values when the context begins and a unique span ID for every span. The root span has no parent; each nested context or decorated function records the active span as its parent. Caller-supplied `run_id` and `trace_id` can be provided to `client.run(...)` when the caller already owns stable identifiers.

Call `run.finish(...)` with an explicit terminal status: `SUCCEEDED`, `FAILED`, `CANCELLED`, or `PARTIAL`. A clean context exit without `finish()` is recorded as `PARTIAL`; it is never promoted to success just because no exception escaped. An escaping exception marks the run `FAILED`, marks the affected spans `ERROR`, sends the trace, then propagates the original exception. Trace export errors on a clean exit raise `TraceExportError` so telemetry loss is visible.

## Decorate functions

```python
@tracecore.trace_tool(name="AccountLookup", capture_input=True)
def lookup_account(account_id: str) -> dict[str, str]:
    return {"account_id": account_id, "status": "active"}


@tracecore.trace_llm(attributes={"llm.provider": "your-provider", "llm.model": "your-model"})
async def call_model(prompt: str) -> str: ...
```

`trace_agent`, `trace_tool`, and `trace_llm` can be used as `@tracecore.trace_tool` or `@tracecore.trace_tool(...)`. They support synchronous and asynchronous functions, preserve the wrapped function metadata, and require an active run. By default, decorators record span timing, type, name, and the attributes supplied in code; they do not capture function arguments or return values. `capture_input=True` and `capture_output=True` enable those values for that decorator.

For returned tool errors that do not raise exceptions, use an explicit span context and set its status:

```python
with tracecore.span("PaymentTool", span_type="TOOL") as tool_span:
    result = payment_tool()
    if not result.ok:
        tool_span.set_status("ERROR", error=result.error)
```

## Privacy and serialization

The SDK converts common Python values to JSON-safe data and applies TraceCore's credential redactor before HTTP transmission. Common credential-key fields and recognizable token patterns are redacted. Decorator input/output capture is disabled by default. This redactor is not comprehensive PII detection and does not replace an application's data-classification controls.

The exporter does not calculate costs, fabricate token counts, or infer model/provider usage. Set metadata or span attributes explicitly when the application has verified values to report. Span and run durations are derived by TraceCore from the recorded start and end timestamps.

## Async behavior

Decorated async functions work inside the ordinary `with client.run(...)` context. The exporter currently uses synchronous HTTPX at run exit, so completion may briefly block the calling event loop. An async exporter and async run context are not part of this SDK version.
