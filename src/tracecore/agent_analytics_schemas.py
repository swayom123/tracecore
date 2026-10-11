from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tracecore.schemas import require_timezone

Severity = Literal["INFO", "WARN", "ERROR"]


class AgentAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeated_tool_call_threshold: int = Field(default=3, ge=2, le=1000)
    repeated_failure_threshold: int = Field(default=2, ge=2, le=1000)
    repeated_plan_threshold: int = Field(default=3, ge=2, le=1000)
    excessive_step_count_threshold: int = Field(default=20, ge=1, le=10000)
    repeated_tool_call_severity: Severity = "WARN"
    repeated_failure_severity: Severity = "ERROR"
    repeated_plan_severity: Severity = "WARN"
    excessive_step_count_severity: Severity = "WARN"


class AgentAnalyticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: AgentAnalysisConfig = Field(default_factory=AgentAnalysisConfig)


class AgentAnalyticsFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    application_id: str | None = Field(default=None, max_length=64)
    agent_name: str | None = Field(default=None, max_length=200)
    agent_version: str | None = Field(default=None, max_length=100)
    started_after: datetime | None = None
    started_before: datetime | None = None

    @field_validator("started_after", "started_before")
    @classmethod
    def timestamps_have_timezone(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else value

    @model_validator(mode="after")
    def range_is_valid(self) -> "AgentAnalyticsFilters":
        if self.started_after and self.started_before and self.started_after > self.started_before:
            raise ValueError("started_after must be less than or equal to started_before")
        return self
