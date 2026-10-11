"""TraceCore ingestion service and opt-in Python instrumentation SDK."""

from tracecore.sdk import (
    RunContext,
    SpanContext,
    TraceCoreClient,
    TraceExportError,
    init,
    run,
    span,
    trace_agent,
    trace_llm,
    trace_tool,
)

__version__ = "0.1.0"

__all__ = [
    "RunContext",
    "SpanContext",
    "TraceCoreClient",
    "TraceExportError",
    "init",
    "run",
    "span",
    "trace_agent",
    "trace_llm",
    "trace_tool",
]
