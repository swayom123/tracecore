from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvaluatorType = Literal[
    "EXACT_MATCH",
    "REQUIRED_FIELDS",
    "VALID_JSON",
    "REGEX_MATCH",
    "RESPONSE_LENGTH",
    "CITATION_PRESENT",
    "TASK_OUTCOME",
    "LATENCY_THRESHOLD",
    "COST_THRESHOLD",
    "TOOL_CALL_CONSTRAINTS",
    "MODEL_JUDGE",
]


class EvaluationCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1, max_length=64)
    input: Any | None = None
    expected_output: Any | None = None
    expected_properties: dict[str, Any] = Field(default_factory=dict)
    reference_context: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    cases: list[EvaluationCaseInput] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_case_ids(self) -> "DatasetCreate":
        ids = [case.id for case in self.cases if case.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("case IDs must be unique within a dataset version")
        return self


class EvaluatorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    evaluator_type: EvaluatorType
    config: dict[str, Any] = Field(default_factory=dict)


class CaseRunMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128)


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1, max_length=64)
    evaluator_ids: list[str] = Field(min_length=1, max_length=50)
    case_runs: list[CaseRunMapping] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_references(self) -> "EvaluationCreate":
        if len(self.evaluator_ids) != len(set(self.evaluator_ids)):
            raise ValueError("evaluator_ids must be unique")
        case_ids = [mapping.case_id for mapping in self.case_runs]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("each case may be mapped to only one run")
        return self
