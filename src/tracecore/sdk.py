"""Explicit context-manager and decorator instrumentation for TraceCore."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from functools import wraps
from types import TracebackType
from typing import Any, ParamSpec, Self, TypeVar, cast, overload
from uuid import uuid4

import httpx
from pydantic import BaseModel

from tracecore.redaction import BasicRedactionService
from tracecore.schemas import RunStatus, SpanStatus, SpanType

P = ParamSpec("P")
R = TypeVar("R")
_UNSET = object()

_ACTIVE_RUN: ContextVar[RunContext | None] = ContextVar("tracecore_active_run", default=None)
_ACTIVE_SPANS: ContextVar[tuple[_SpanRecord, ...]] = ContextVar(
    "tracecore_active_spans", default=()
)
_DEFAULT_CLIENT: TraceCoreClient | None = None


class TraceExportError(RuntimeError):
    """Raised when a completed trace cannot be delivered to TraceCore."""


def _now() -> datetime:
    return datetime.now(UTC)


def _json_safe(value: Any) -> Any:
    """Convert common Python values to bounded JSON-compatible data without calling repr()."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"content_type": "bytes", "length": len(value)}
    return {"__type__": f"{type(value).__module__}.{type(value).__qualname__}"}


class TraceCoreClient:
    """A small synchronous exporter bound to one project and application."""

    def __init__(
        self,
        endpoint: str,
        *,
        project_id: str,
        application_id: str,
        environment: str = "development",
        project_name: str | None = None,
        application_name: str | None = None,
        timeout: float = 5.0,
        headers: Mapping[str, str] | None = None,
        redaction_patterns: Sequence[str] = (),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must be an absolute HTTP or HTTPS URL")
        if not project_id or not application_id or not environment:
            raise ValueError("project_id, application_id, and environment are required")
        self.endpoint = endpoint.rstrip("/")
        self.project_id = project_id
        self.application_id = application_id
        self.project_name = project_name
        self.application_name = application_name
        self.environment = environment
        if isinstance(redaction_patterns, str):
            raise TypeError("redaction_patterns must be a sequence of regex strings")
        self._redactor = BasicRedactionService(json.dumps(list(redaction_patterns)))
        self._http = httpx.Client(
            base_url=self.endpoint,
            headers=dict(headers or {}),
            timeout=timeout,
            transport=transport,
        )

    def run(
        self,
        *,
        agent_name: str | None = None,
        agent_version: str | None = None,
        workflow_name: str | None = None,
        name: str | None = None,
        input: Any = None,
        metadata: Mapping[str, Any] | None = None,
        tags: list[str] | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> RunContext:
        return RunContext(
            client=self,
            agent_name=agent_name,
            agent_version=agent_version,
            workflow_name=workflow_name,
            name=name,
            input=input,
            metadata=metadata,
            tags=tags,
            run_id=run_id,
            trace_id=trace_id,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _redact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        run = dict(result["run"])
        for field in ("input", "output", "metadata", "model_usage", "token_usage", "tags"):
            run[field] = self._redactor.redact(_json_safe(run.get(field)))
        result["run"] = run
        result["attributes"] = self._redactor.redact(_json_safe(result.get("attributes", {})))
        redacted_spans: list[dict[str, Any]] = []
        for span in result["spans"]:
            item = dict(span)
            for field in ("input", "output", "attributes", "error"):
                item[field] = self._redactor.redact(_json_safe(item.get(field)))
            events = []
            for event in item.get("events", []):
                safe_event = dict(event)
                safe_event["attributes"] = self._redactor.redact(
                    _json_safe(safe_event.get("attributes", {}))
                )
                events.append(safe_event)
            item["events"] = events
            redacted_spans.append(item)
        result["spans"] = redacted_spans
        return result

    def _send(self, payload: dict[str, Any]) -> None:
        safe_payload = self._redact_payload(payload)
        try:
            response = self._http.post("/v1/traces", json=safe_payload)
        except httpx.HTTPError as exc:
            raise TraceExportError(
                "TraceCore trace export failed before receiving a response"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise TraceExportError(f"TraceCore rejected the trace with HTTP {response.status_code}")


class _SpanRecord:
    def __init__(
        self,
        *,
        span_id: str,
        parent_span_id: str | None,
        span_type: SpanType,
        name: str,
        started_at: datetime,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.span_type = span_type
        self.name = name
        self.started_at = started_at
        self.ended_at: datetime | None = None
        self.status = SpanStatus.UNSET
        self.input: Any = None
        self.output: Any = None
        self.attributes = dict(attributes or {})
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "span_type": self.span_type.value,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "status": self.status.value,
            "input": _json_safe(self.input),
            "output": _json_safe(self.output),
            "attributes": _json_safe(self.attributes),
            "error": self.error,
            "events": self.events,
        }


class SpanContext:
    """One explicit span. Normal exit marks the span OK; exceptions mark it ERROR."""

    def __init__(
        self,
        name: str,
        *,
        span_type: SpanType | str = SpanType.CUSTOM,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if not name:
            raise ValueError("span name must not be empty")
        self.name = name
        self.span_type = _span_type(span_type)
        self.attributes = dict(attributes or {})
        self._record: _SpanRecord | None = None
        self._stack_token: Token[tuple[_SpanRecord, ...]] | None = None

    @property
    def span_id(self) -> str:
        return self._require_record().span_id

    @property
    def parent_span_id(self) -> str | None:
        return self._require_record().parent_span_id

    def __enter__(self) -> Self:
        run_context = _ACTIVE_RUN.get()
        if run_context is None:
            raise RuntimeError("TraceCore span used outside an active `client.run(...)` context")
        stack = _ACTIVE_SPANS.get()
        parent_id = stack[-1].span_id if stack else None
        record = _SpanRecord(
            span_id=str(uuid4()),
            parent_span_id=parent_id,
            span_type=self.span_type,
            name=self.name,
            started_at=_now(),
            attributes=self.attributes,
        )
        self._record = record
        run_context._spans.append(record)
        self._stack_token = _ACTIVE_SPANS.set((*stack, record))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        record = self._require_record()
        record.ended_at = _now()
        if exc is not None:
            record.status = SpanStatus.ERROR
            record.error = f"{type(exc).__name__}: {exc}"
        elif record.status is SpanStatus.UNSET:
            record.status = SpanStatus.OK
        if self._stack_token is not None:
            _ACTIVE_SPANS.reset(self._stack_token)
            self._stack_token = None

    def set_input(self, value: Any) -> None:
        self._require_record().input = value

    def set_output(self, value: Any) -> None:
        self._require_record().output = value

    def set_attribute(self, key: str, value: Any) -> None:
        self._require_record().attributes[key] = value

    def set_status(self, status: SpanStatus | str, *, error: str | None = None) -> None:
        record = self._require_record()
        record.status = SpanStatus(str(status).upper())
        if error is not None:
            record.error = error

    def record_event(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        if not name:
            raise ValueError("event name must not be empty")
        event_time = timestamp or _now()
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError("event timestamp must include a timezone")
        self._require_record().events.append(
            {
                "name": name,
                "timestamp": event_time.isoformat(),
                "attributes": _json_safe(dict(attributes or {})),
            }
        )

    def _require_record(self) -> _SpanRecord:
        if self._record is None:
            raise RuntimeError("span operation requires entering the span context first")
        return self._record


class RunContext:
    """A run records a real span tree and exports once when its context exits."""

    def __init__(
        self,
        *,
        client: TraceCoreClient,
        agent_name: str | None,
        agent_version: str | None,
        workflow_name: str | None,
        name: str | None,
        input: Any,
        metadata: Mapping[str, Any] | None,
        tags: list[str] | None,
        run_id: str | None,
        trace_id: str | None,
    ) -> None:
        self.client = client
        self.run_id = run_id or str(uuid4())
        self.trace_id = trace_id or str(uuid4())
        self.agent_name = agent_name
        self.agent_version = agent_version
        self.workflow_name = workflow_name
        self.name = name or agent_name or workflow_name or "Run"
        self.input = input
        self.output: Any = None
        self.metadata = dict(metadata or {})
        self.tags = list(tags or [])
        self.model_usage: dict[str, Any] = {}
        self.token_usage: dict[str, Any] = {}
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self.status: RunStatus | None = None
        self.error: str | None = None
        self._outcome_explicit = False
        self._spans: list[_SpanRecord] = []
        self._run_token: Token[RunContext | None] | None = None
        self._stack_token: Token[tuple[_SpanRecord, ...]] | None = None

    @property
    def root_span_id(self) -> str:
        if not self._spans:
            raise RuntimeError("run context has not been entered")
        return self._spans[0].span_id

    def __enter__(self) -> Self:
        if _ACTIVE_RUN.get() is not None:
            raise RuntimeError(
                "nested TraceCore runs are not supported; create a child span instead"
            )
        self.started_at = _now()
        self._run_token = _ACTIVE_RUN.set(self)
        root = _SpanRecord(
            span_id=str(uuid4()),
            parent_span_id=None,
            span_type=SpanType.AGENT if self.agent_name else SpanType.WORKFLOW,
            name=self.name,
            started_at=self.started_at,
            attributes={"agent.version": self.agent_version} if self.agent_version else {},
        )
        self._spans.append(root)
        self._stack_token = _ACTIVE_SPANS.set((root,))
        return self

    def finish(
        self,
        status: RunStatus | str,
        *,
        output: Any = _UNSET,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        model_usage: Mapping[str, Any] | None = None,
        token_usage: Mapping[str, Any] | None = None,
    ) -> None:
        if self.started_at is None:
            raise RuntimeError("finish() must be called inside an active run context")
        if self._outcome_explicit:
            raise RuntimeError("run outcome has already been set")
        final_status = _run_status(status)
        if final_status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            raise ValueError("finish() requires a terminal run status")
        self.status = final_status
        self.error = error
        if output is not _UNSET:
            self.output = output
        if metadata is not None:
            self.metadata.update(metadata)
        if model_usage is not None:
            self.model_usage = dict(model_usage)
        if token_usage is not None:
            self.token_usage = dict(token_usage)
        self._outcome_explicit = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.started_at is None:
            raise RuntimeError("run context was not entered")
        self.completed_at = _now()
        root = self._spans[0]
        if exc is not None:
            self.status = RunStatus.FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            root.status = SpanStatus.ERROR
            root.error = self.error
        elif self.status is None:
            # A clean block exit is not proof of task completion.
            self.status = RunStatus.PARTIAL
            root.status = SpanStatus.UNSET
        elif self.status is RunStatus.FAILED:
            root.status = SpanStatus.ERROR
            root.error = self.error
        elif self.status in {RunStatus.CANCELLED, RunStatus.PARTIAL}:
            root.status = SpanStatus.UNSET
            root.error = self.error
        else:
            root.status = SpanStatus.OK
        root.ended_at = self.completed_at

        payload = self._payload()
        export_error: Exception | None = None
        try:
            self.client._send(payload)
        except Exception as error:  # noqa: BLE001 - preserve application exceptions during export.
            export_error = error
        finally:
            if self._stack_token is not None:
                _ACTIVE_SPANS.reset(self._stack_token)
                self._stack_token = None
            if self._run_token is not None:
                _ACTIVE_RUN.reset(self._run_token)
                self._run_token = None
        if export_error is not None:
            if exc is not None:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"TraceCore telemetry export also failed: {type(export_error).__name__}"
                    )
            elif isinstance(export_error, TraceExportError):
                raise export_error
            else:
                raise TraceExportError("TraceCore trace export failed") from export_error

    def _payload(self) -> dict[str, Any]:
        assert (
            self.started_at is not None
            and self.completed_at is not None
            and self.status is not None
        )
        return {
            "run": {
                "run_id": self.run_id,
                "project_id": self.client.project_id,
                "project_name": self.client.project_name,
                "application_id": self.client.application_id,
                "application_name": self.client.application_name,
                "environment": self.client.environment,
                "agent_name": self.agent_name,
                "agent_version": self.agent_version,
                "workflow_name": self.workflow_name,
                "trace_id": self.trace_id,
                "status": self.status.value,
                "started_at": self.started_at.isoformat(),
                "completed_at": self.completed_at.isoformat(),
                "input": _json_safe(self.input),
                "output": _json_safe(self.output),
                "model_usage": _json_safe(self.model_usage),
                "token_usage": _json_safe(self.token_usage),
                "tags": _json_safe(self.tags),
                "metadata": _json_safe(self.metadata),
            },
            "root_span_id": self.root_span_id,
            "spans": [span.to_payload() for span in self._spans],
        }


def _span_type(value: SpanType | str) -> SpanType:
    if isinstance(value, SpanType):
        return value
    return SpanType(str(value).upper())


def _run_status(value: RunStatus | str) -> RunStatus:
    if isinstance(value, RunStatus):
        return value
    return RunStatus(str(value).upper())


def span(
    name: str,
    *,
    span_type: SpanType | str = SpanType.CUSTOM,
    attributes: Mapping[str, Any] | None = None,
) -> SpanContext:
    """Create a child span inside the current run."""
    return SpanContext(name, span_type=span_type, attributes=attributes)


def _captured_arguments(
    function: Callable[P, Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        bound = inspect.signature(function).bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return {"argument_count": len(args), "keyword_names": sorted(kwargs)}


def _instrument(
    span_type: SpanType,
    function: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]] | Callable[P, R]:
    def decorate(target: Callable[P, R]) -> Callable[P, R]:
        span_name = name or target.__qualname__

        if inspect.iscoroutinefunction(target):

            @wraps(target)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                with SpanContext(
                    span_name, span_type=span_type, attributes=attributes
                ) as current_span:
                    if capture_input:
                        current_span.set_input(_captured_arguments(target, args, dict(kwargs)))
                    result = await target(*args, **kwargs)
                    if capture_output:
                        current_span.set_output(result)
                    return result

            return cast(Callable[P, R], async_wrapper)

        @wraps(target)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with SpanContext(span_name, span_type=span_type, attributes=attributes) as current_span:
                if capture_input:
                    current_span.set_input(_captured_arguments(target, args, dict(kwargs)))
                result = target(*args, **kwargs)
                if capture_output:
                    current_span.set_output(result)
                return result

        return wrapper

    if function is not None:
        return decorate(function)
    return decorate


@overload
def trace_agent(function: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def trace_agent(
    function: None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def trace_agent(
    function: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Any:
    return _instrument(
        SpanType.AGENT,
        function,
        name=name,
        attributes=attributes,
        capture_input=capture_input,
        capture_output=capture_output,
    )


@overload
def trace_tool(function: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def trace_tool(
    function: None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def trace_tool(
    function: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Any:
    return _instrument(
        SpanType.TOOL,
        function,
        name=name,
        attributes=attributes,
        capture_input=capture_input,
        capture_output=capture_output,
    )


@overload
def trace_llm(function: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def trace_llm(
    function: None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def trace_llm(
    function: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
) -> Any:
    return _instrument(
        SpanType.LLM,
        function,
        name=name,
        attributes=attributes,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def init(
    endpoint: str,
    *,
    project_id: str,
    application_id: str,
    environment: str = "development",
    project_name: str | None = None,
    application_name: str | None = None,
    timeout: float = 5.0,
    headers: Mapping[str, str] | None = None,
) -> TraceCoreClient:
    """Configure the module-level client used by :func:`run`."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is not None:
        _DEFAULT_CLIENT.close()
    _DEFAULT_CLIENT = TraceCoreClient(
        endpoint,
        project_id=project_id,
        application_id=application_id,
        environment=environment,
        project_name=project_name,
        application_name=application_name,
        timeout=timeout,
        headers=headers,
    )
    return _DEFAULT_CLIENT


def run(**kwargs: Any) -> RunContext:
    """Start a run using the client configured by :func:`init`."""
    if _DEFAULT_CLIENT is None:
        raise RuntimeError("call tracecore.init(...) or use TraceCoreClient before starting a run")
    return _DEFAULT_CLIENT.run(**kwargs)
