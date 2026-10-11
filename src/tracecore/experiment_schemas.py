from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ExperimentMetric = Literal[
    "evaluation_pass_rate",
    "failure_count",
    "latency_ms",
    "recorded_cost",
    "tool_calls",
]


class ExperimentCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    configuration: dict[str, Any] = Field(min_length=1)
    case_runs: dict[str, str] = Field(min_length=1)


class ExperimentCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: ExperimentMetric
    direction: Literal["LOWER", "HIGHER"]
    max_regression: float = Field(ge=0, allow_inf_nan=False)
    severity: Literal["WARN", "FAIL"] = "FAIL"

    @model_validator(mode="after")
    def direction_matches_metric(self) -> "ExperimentCriterion":
        lower_metrics = {"failure_count", "latency_ms", "recorded_cost", "tool_calls"}
        expected = "LOWER" if self.metric in lower_metrics else "HIGHER"
        if self.direction != expected:
            raise ValueError(f"{self.metric} requires direction {expected}")
        return self


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=64)
    evaluator_ids: list[str] = Field(min_length=1, max_length=50)
    baseline_candidate_id: str = Field(min_length=1, max_length=64)
    candidates: list[ExperimentCandidateInput] = Field(min_length=2, max_length=20)
    criteria: list[ExperimentCriterion] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_ids(self) -> "ExperimentCreate":
        candidate_ids = [item.id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        if self.baseline_candidate_id not in candidate_ids:
            raise ValueError("baseline_candidate_id must identify a candidate")
        if len(self.evaluator_ids) != len(set(self.evaluator_ids)):
            raise ValueError("evaluator_ids must be unique")
        metrics = [item.metric for item in self.criteria]
        if len(metrics) != len(set(metrics)):
            raise ValueError("each comparison metric may have only one criterion")
        return self
