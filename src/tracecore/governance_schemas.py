from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tracecore.schemas import require_timezone

PolicyField = Literal[
    "environment",
    "agent_name",
    "agent_version",
    "run_status",
    "logging_mode",
    "model",
    "prompt_version",
    "span_status",
    "tool_name",
]
PolicyOperator = Literal["eq", "neq", "in", "not_in", "exists", "not_exists"]
PolicyEffect = Literal["ALLOW", "WARN", "BLOCK", "REQUIRE_REVIEW"]
ViolationStatus = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"]


class PolicyConditionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: PolicyField
    operator: PolicyOperator
    value: Any = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "PolicyConditionInput":
        if self.operator in {"exists", "not_exists"}:
            if self.value is not None:
                raise ValueError(f"{self.operator} does not accept a value")
        elif self.operator in {"in", "not_in"}:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.operator} needs a non-empty list value")
            if any(not isinstance(item, (str, int, float, bool)) for item in self.value):
                raise ValueError("condition values must be scalar strings or numbers")
        elif not isinstance(self.value, (str, int, float, bool)):
            raise ValueError(f"{self.operator} needs a scalar value")
        return self


class PolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    conditions: list[PolicyConditionInput] = Field(min_length=1, max_length=50)
    effect: PolicyEffect
    severity: Literal["INFO", "WARN", "ERROR", "CRITICAL"] = "WARN"


class PolicyCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_ids: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def unique_policy_ids(self) -> "PolicyCheckRequest":
        if self.policy_ids is not None and len(self.policy_ids) != len(set(self.policy_ids)):
            raise ValueError("policy_ids must be unique")
        return self


class ViolationTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ViolationStatus
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expiration_has_timezone(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else value

    @model_validator(mode="after")
    def waiver_fields_are_consistent(self) -> "ViolationTransitionRequest":
        if self.status == "WAIVED" and self.expires_at is None:
            raise ValueError("WAIVED transitions require expires_at")
        if self.status != "WAIVED" and self.expires_at is not None:
            raise ValueError("expires_at is only valid for WAIVED transitions")
        return self
