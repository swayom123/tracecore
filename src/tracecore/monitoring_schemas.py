import math
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MonitoringScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str | None = None
    application_id: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None


class BudgetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    scope: MonitoringScope = Field(default_factory=MonitoringScope)
    limits: dict[
        Literal["model_calls", "tool_calls", "tokens", "duration_ms", "verified_cost", "retries"],
        float,
    ]

    @model_validator(mode="after")
    def validate_limits(self) -> "BudgetCreate":
        if not self.limits or any(
            value < 0 or not math.isfinite(value) for value in self.limits.values()
        ):
            raise ValueError("limits must contain non-negative values")
        return self


class AlertRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    metric: Literal[
        "failure_rate",
        "critical_failure_rate",
        "runaway_loops",
        "policy_violations",
        "verified_cost",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
    ]
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float
    window_minutes: int = Field(ge=1, le=10080)
    minimum_samples: int = Field(default=1, ge=1, le=1000000)
    severity: Literal["INFO", "WARN", "ERROR", "CRITICAL"]
    scope: MonitoringScope = Field(default_factory=MonitoringScope)
    destination: dict[str, Any]
    cooldown_seconds: int = Field(default=0, ge=0, le=604800)
    grouping_key: Literal["rule", "environment", "application", "agent", "version"] = "rule"

    @model_validator(mode="after")
    def destination_valid(self) -> "AlertRuleCreate":
        if not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite")
        if (
            self.metric in {"failure_rate", "critical_failure_rate"}
            and not 0 <= self.threshold <= 1
        ):
            raise ValueError("rate thresholds must be between 0 and 1")
        if self.metric not in {"failure_rate", "critical_failure_rate"} and self.threshold < 0:
            raise ValueError("threshold must not be negative")
        kind = self.destination.get("type")
        if kind not in {"console", "database", "webhook"}:
            raise ValueError("destination.type must be console, database, or webhook")
        allowed_keys = {"type", "url"} if kind == "webhook" else {"type"}
        if set(self.destination) - allowed_keys:
            raise ValueError("destination contains unsupported configuration fields")
        if kind == "webhook":
            url = self.destination.get("url")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (
                parsed is None
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("webhook destination requires an http(s) URL")
        return self


class AlertResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["RESOLVED"]
