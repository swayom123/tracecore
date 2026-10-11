from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PARTIAL = "PARTIAL"


class SpanType(StrEnum):
    AGENT = "AGENT"
    LLM = "LLM"
    TOOL = "TOOL"
    RETRIEVAL = "RETRIEVAL"
    PLANNER = "PLANNER"
    WORKFLOW = "WORKFLOW"
    HTTP = "HTTP"
    DATABASE = "DATABASE"
    GUARDRAIL = "GUARDRAIL"
    VERIFICATION = "VERIFICATION"
    CUSTOM = "CUSTOM"


class SpanStatus(StrEnum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    timestamp: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)

    _timezone = field_validator("timestamp")(require_timezone)


class SpanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    span_id: str = Field(min_length=1, max_length=128, alias="id")
    parent_span_id: str | None = Field(default=None, max_length=128)
    span_type: SpanType
    name: str = Field(min_length=1, max_length=300)
    started_at: datetime
    ended_at: datetime | None = None
    status: SpanStatus = SpanStatus.UNSET
    input: Any | None = None
    output: Any | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=20_000)
    events: list[EventInput] = Field(default_factory=list, max_length=1000)

    _timezone = field_validator("started_at", "ended_at")(require_timezone)

    @model_validator(mode="after")
    def timestamps_in_order(self) -> "SpanInput":
        if self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at must be greater than or equal to started_at")
        return self


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=64)
    project_name: str | None = Field(default=None, max_length=200)
    application_id: str = Field(min_length=1, max_length=64)
    application_name: str | None = Field(default=None, max_length=200)
    environment: str = Field(min_length=1, max_length=80)
    agent_name: str | None = Field(default=None, max_length=200)
    agent_version: str | None = Field(default=None, max_length=100)
    workflow_name: str | None = Field(default=None, max_length=200)
    trace_id: str = Field(min_length=1, max_length=128)
    status: RunStatus
    started_at: datetime
    completed_at: datetime | None = None
    input: Any | None = None
    output: Any | None = None
    model_usage: dict[str, Any] = Field(default_factory=dict)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    estimated_cost: float | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _timezone = field_validator("started_at", "completed_at")(require_timezone)

    @model_validator(mode="after")
    def timestamps_in_order(self) -> "RunInput":
        if self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at must be greater than or equal to started_at")
        if (
            self.status
            in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PARTIAL}
            and not self.completed_at
        ):
            raise ValueError("completed_at is required for a completed run status")
        return self


class TracePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: RunInput
    root_span_id: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)
    spans: list[SpanInput] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_relationships(self) -> "TracePayload":
        ids = [span.span_id for span in self.spans]
        if len(ids) != len(set(ids)):
            raise ValueError("span IDs must be unique within a trace")
        span_ids = set(ids)
        if self.root_span_id not in span_ids:
            raise ValueError(f"root span `{self.root_span_id}` does not exist")
        roots = [span for span in self.spans if span.parent_span_id is None]
        if len(roots) != 1 or roots[0].span_id != self.root_span_id:
            raise ValueError("the declared root span must be the only span without a parent")
        for span in self.spans:
            if span.parent_span_id and span.parent_span_id not in span_ids:
                raise ValueError(
                    f"span `{span.span_id}` references unknown parent `{span.parent_span_id}`"
                )
            if span.parent_span_id == span.span_id:
                raise ValueError(f"span `{span.span_id}` cannot be its own parent")
        _validate_acyclic(self.spans)
        return self


class BatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    traces: list[TracePayload] = Field(min_length=1, max_length=100)


class IngestResponse(BaseModel):
    accepted: list[str]
    rejected: list[dict[str, str]] = Field(default_factory=list)


def _validate_acyclic(spans: list[SpanInput]) -> None:
    parents = {span.span_id: span.parent_span_id for span in spans}
    for span_id in parents:
        seen: set[str] = set()
        current: str | None = span_id
        while current is not None:
            if current in seen:
                raise ValueError(f"span parent relationships contain a cycle at `{current}`")
            seen.add(current)
            current = parents[current]
