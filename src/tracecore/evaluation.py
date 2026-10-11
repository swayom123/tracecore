"""Deterministic, data-only evaluation of explicitly mapped stored runs."""

import json
import math
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracecore.evaluation_schemas import (
    DatasetCreate,
    EvaluationCreate,
    EvaluatorCreate,
)
from tracecore.model_evaluation import (
    JudgeConfigurationError,
    JudgeProvider,
    ModelBasedEvaluationEngine,
    validate_model_judge_config,
)
from tracecore.models import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationResult,
    EvaluationRun,
    EvaluatorDefinition,
    Run,
    Span,
    utc_now,
)
from tracecore.redaction import BasicRedactionService
from tracecore.services import RedactionService


class EvaluationConfigurationError(ValueError):
    pass


SUPPORTED_EVALUATORS = {
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
}


def validate_evaluator_config(evaluator_type: str, config: dict[str, Any]) -> None:
    allowed: dict[str, set[str]] = {
        "EXACT_MATCH": {"output_path"},
        "REQUIRED_FIELDS": {"fields", "output_path"},
        "VALID_JSON": {"output_path"},
        "REGEX_MATCH": {"pattern", "output_path"},
        "RESPONSE_LENGTH": {"min_chars", "max_chars", "output_path"},
        "CITATION_PRESENT": {"pattern", "output_path"},
        "TASK_OUTCOME": set(),
        "LATENCY_THRESHOLD": {"max_ms"},
        "COST_THRESHOLD": {"max_cost"},
        "TOOL_CALL_CONSTRAINTS": {"min_calls", "max_calls", "allowed_tools", "required_tools"},
        "MODEL_JUDGE": {
            "provider",
            "model",
            "dimension",
            "prompt_version",
            "timeout_seconds",
            "max_input_chars",
        },
    }
    if evaluator_type not in SUPPORTED_EVALUATORS:
        raise EvaluationConfigurationError(f"Unsupported evaluator type `{evaluator_type}`")
    unexpected = set(config) - allowed[evaluator_type]
    if unexpected:
        raise EvaluationConfigurationError(
            f"Unsupported config fields for {evaluator_type}: {', '.join(sorted(unexpected))}"
        )
    if evaluator_type == "MODEL_JUDGE":
        try:
            validate_model_judge_config(config)
        except JudgeConfigurationError as exc:
            raise EvaluationConfigurationError(str(exc)) from exc
        return
    output_path = config.get("output_path")
    if output_path is not None and (
        not isinstance(output_path, str)
        or not output_path.strip()
        or any(not part for part in output_path.split("."))
    ):
        raise EvaluationConfigurationError("output_path must be a non-empty dotted object path")
    if evaluator_type == "REQUIRED_FIELDS":
        fields = config.get("fields")
        if (
            not isinstance(fields, list)
            or not fields
            or any(not isinstance(x, str) or not x for x in fields)
        ):
            raise EvaluationConfigurationError("REQUIRED_FIELDS needs a non-empty `fields` list")
    if evaluator_type == "REGEX_MATCH":
        _compile_pattern(config.get("pattern"))
    if evaluator_type == "CITATION_PRESENT" and "pattern" in config:
        _compile_pattern(config["pattern"])
    if evaluator_type == "RESPONSE_LENGTH":
        minimum, maximum = config.get("min_chars"), config.get("max_chars")
        if minimum is None and maximum is None:
            raise EvaluationConfigurationError("RESPONSE_LENGTH needs min_chars or max_chars")
        for label, value in (("min_chars", minimum), ("max_chars", maximum)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise EvaluationConfigurationError(f"{label} must be a non-negative integer")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise EvaluationConfigurationError("min_chars must be less than or equal to max_chars")
    if evaluator_type == "LATENCY_THRESHOLD":
        _finite_nonnegative(config.get("max_ms"), "max_ms")
    if evaluator_type == "COST_THRESHOLD":
        _finite_nonnegative(config.get("max_cost"), "max_cost")
    if evaluator_type == "TOOL_CALL_CONSTRAINTS":
        minimum, maximum = config.get("min_calls"), config.get("max_calls")
        if (
            minimum is None
            and maximum is None
            and not config.get("allowed_tools")
            and not config.get("required_tools")
        ):
            raise EvaluationConfigurationError(
                "TOOL_CALL_CONSTRAINTS needs at least one constraint"
            )
        for label, value in (("min_calls", minimum), ("max_calls", maximum)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise EvaluationConfigurationError(f"{label} must be a non-negative integer")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise EvaluationConfigurationError("min_calls must be less than or equal to max_calls")
        for label in ("allowed_tools", "required_tools"):
            value = config.get(label)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item for item in value)
            ):
                raise EvaluationConfigurationError(f"{label} must be a list of tool names")


def _finite_nonnegative(value: Any, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise EvaluationConfigurationError(f"{field} must be a finite non-negative number")


def _compile_pattern(value: Any) -> re.Pattern[str]:
    if not isinstance(value, str) or not value:
        raise EvaluationConfigurationError("pattern must be a non-empty regular expression")
    if len(value) > 256:
        raise EvaluationConfigurationError("pattern must be 256 characters or fewer")
    if any(token in value for token in ("(", ")", "|")):
        raise EvaluationConfigurationError(
            "pattern uses grouping or alternation, which are not supported by the safe regex subset"
        )
    if ".*" in value or ".+" in value:
        raise EvaluationConfigurationError("unbounded wildcard patterns are not supported")
    quantifiers = sum(value.count(token) for token in ("*", "+", "?")) + value.count("{")
    if quantifiers > 1:
        raise EvaluationConfigurationError("pattern may contain at most one repetition operator")
    try:
        return re.compile(value)
    except re.error as exc:
        raise EvaluationConfigurationError(f"pattern is invalid: {exc}") from exc


def _at_path(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _content_omitted(value: Any) -> bool:
    return isinstance(value, dict) and value.get("content_omitted") is True


_MISSING = object()


def _result(status: str, reason: str, **details: Any) -> tuple[str, str, dict[str, Any]]:
    return status, reason, details


class DeterministicEvaluationEngine:
    """Evaluates stored JSON telemetry; never executes candidate or evaluator code."""

    def evaluate(
        self,
        evaluator: EvaluatorDefinition,
        case: EvaluationCase,
        run: Run | None,
        spans: list[Span],
    ) -> tuple[str, str, dict[str, Any]]:
        kind, config = evaluator.evaluator_type, evaluator.config
        if run is None:
            return _result("UNAVAILABLE", "No ingested run was assigned to this case")
        if kind == "TASK_OUTCOME":
            passed = run.status == "SUCCEEDED"
            return _result(
                "PASSED" if passed else "FAILED",
                f"Recorded run outcome is {run.status}",
                run_status=run.status,
            )
        if kind == "LATENCY_THRESHOLD":
            if run.duration_ms is None:
                return _result("UNAVAILABLE", "Run duration was not recorded")
            passed = run.duration_ms <= config["max_ms"]
            return _result(
                "PASSED" if passed else "FAILED",
                f"Run duration {run.duration_ms:g} ms {'is within' if passed else 'exceeds'} the {config['max_ms']:g} ms limit",
                duration_ms=run.duration_ms,
                limit_ms=config["max_ms"],
            )
        if kind == "COST_THRESHOLD":
            if run.estimated_cost is None:
                return _result(
                    "UNAVAILABLE", "Cost unavailable: no verified cost was recorded for this run"
                )
            passed = run.estimated_cost <= config["max_cost"]
            return _result(
                "PASSED" if passed else "FAILED",
                f"Recorded cost {'is within' if passed else 'exceeds'} the configured limit",
                cost=run.estimated_cost,
                limit=config["max_cost"],
            )
        if kind == "TOOL_CALL_CONSTRAINTS":
            tools = [span.name for span in spans if span.span_type == "TOOL"]
            if "min_calls" in config and len(tools) < config["min_calls"]:
                return _result(
                    "FAILED",
                    f"Observed {len(tools)} tool calls; minimum is {config['min_calls']}",
                    tool_names=tools,
                )
            if "max_calls" in config and len(tools) > config["max_calls"]:
                return _result(
                    "FAILED",
                    f"Observed {len(tools)} tool calls; maximum is {config['max_calls']}",
                    tool_names=tools,
                )
            if "allowed_tools" in config:
                disallowed = sorted(set(tools) - set(config["allowed_tools"]))
                if disallowed:
                    return _result(
                        "FAILED",
                        f"Disallowed tools were called: {', '.join(disallowed)}",
                        tool_names=tools,
                        disallowed=disallowed,
                    )
            missing = sorted(set(config.get("required_tools", [])) - set(tools))
            if missing:
                return _result(
                    "FAILED",
                    f"Required tools were not called: {', '.join(missing)}",
                    tool_names=tools,
                    missing=missing,
                )
            return _result(
                "PASSED",
                f"Observed {len(tools)} tool calls; configured constraints passed",
                tool_names=tools,
            )

        candidate = _at_path(run.output_reference, config.get("output_path"))
        if candidate is _MISSING or candidate is None:
            return _result(
                "UNAVAILABLE", "Run output was not recorded at the configured output path"
            )
        if _content_omitted(candidate):
            return _result("UNAVAILABLE", "Run output content was omitted by the logging mode")
        if kind == "EXACT_MATCH":
            if not case.expected_output_present or _content_omitted(case.expected_output):
                return _result("UNAVAILABLE", "Expected output was not recorded for this case")
            passed = candidate == case.expected_output
            return _result(
                "PASSED" if passed else "FAILED",
                "Output exactly matches expected_output"
                if passed
                else "Output does not exactly match expected_output",
                expected_type=type(case.expected_output).__name__,
                actual_type=type(candidate).__name__,
            )
        if kind == "REQUIRED_FIELDS":
            if not isinstance(candidate, dict):
                return _result(
                    "FAILED",
                    "Candidate output is not an object",
                    actual_type=type(candidate).__name__,
                )
            missing = [field for field in config["fields"] if field not in candidate]
            return _result(
                "FAILED" if missing else "PASSED",
                f"Missing required fields: {', '.join(missing)}"
                if missing
                else "All required fields are present",
                missing_fields=missing,
            )
        if kind == "VALID_JSON":
            if not isinstance(candidate, str):
                return _result(
                    "PASSED",
                    "Stored structured output is already valid JSON",
                    output_type=type(candidate).__name__,
                )
            try:
                json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                return _result("FAILED", "Candidate output is not valid JSON")
            return _result("PASSED", "Candidate output parses as valid JSON")
        if kind == "REGEX_MATCH":
            if not isinstance(candidate, str):
                return _result("UNAVAILABLE", "Regex evaluation requires a string output")
            matched = bool(_compile_pattern(config["pattern"]).search(candidate))
            return _result(
                "PASSED" if matched else "FAILED",
                "Pattern matched candidate output"
                if matched
                else "Pattern did not match candidate output",
                matched=matched,
            )
        if kind == "RESPONSE_LENGTH":
            if not isinstance(candidate, str):
                return _result("UNAVAILABLE", "Response length evaluation requires a string output")
            length = len(candidate)
            passed = ("min_chars" not in config or length >= config["min_chars"]) and (
                "max_chars" not in config or length <= config["max_chars"]
            )
            return _result(
                "PASSED" if passed else "FAILED",
                f"Response has {length} characters",
                character_count=length,
                min_chars=config.get("min_chars"),
                max_chars=config.get("max_chars"),
            )
        if kind == "CITATION_PRESENT":
            if not isinstance(candidate, str):
                return _result("UNAVAILABLE", "Citation evaluation requires a string output")
            if "pattern" in config:
                found = _compile_pattern(config["pattern"]).search(candidate) is not None
            else:
                found = any(
                    re.search(pattern, candidate) is not None
                    for pattern in (r"\[[^\]]+\]", r"https?://\S+")
                )
            return _result(
                "PASSED" if found else "FAILED",
                "Citation marker found" if found else "No citation marker found",
                citation_present=found,
            )
        raise EvaluationConfigurationError(f"Unsupported evaluator type `{kind}`")


class EvaluationService:
    def __init__(
        self,
        session: Session,
        redactor: RedactionService | None = None,
        *,
        model_provider: JudgeProvider | None = None,
        allowed_model_providers: set[str] | None = None,
        restricted_model_providers: set[str] | None = None,
    ) -> None:
        self.session = session
        self.redactor = redactor or BasicRedactionService()
        self.config_redactor = BasicRedactionService()
        self.engine = DeterministicEvaluationEngine()
        self.model_engine = ModelBasedEvaluationEngine(
            model_provider,
            allowed_providers=allowed_model_providers,
            restricted_data_providers=restricted_model_providers,
        )

    def create_dataset(self, request: DatasetCreate) -> EvaluationDataset:
        version = (
            self.session.scalar(
                select(func.max(EvaluationDataset.version)).where(
                    EvaluationDataset.project_id == request.project_id,
                    EvaluationDataset.name == request.name,
                )
            )
            or 0
        ) + 1
        dataset = EvaluationDataset(
            project_id=request.project_id,
            name=request.name,
            description=request.description,
            version=version,
        )
        self.session.add(dataset)
        self.session.flush()
        for ordinal, item in enumerate(request.cases):
            self.session.add(
                EvaluationCase(
                    id=item.id or None,
                    dataset_id=dataset.id,
                    ordinal=ordinal,
                    input=self.redactor.redact(item.input),
                    expected_output=self.redactor.redact(item.expected_output),
                    expected_output_present="expected_output" in item.model_fields_set,
                    expected_properties=self.redactor.redact(item.expected_properties),
                    reference_context=self.redactor.redact(item.reference_context),
                    metadata_json=self.redactor.redact(item.metadata),
                )
            )
        self.session.commit()
        self.session.refresh(dataset)
        return dataset

    def create_evaluator(self, request: EvaluatorCreate) -> EvaluatorDefinition:
        validate_evaluator_config(request.evaluator_type, request.config)
        config = dict(request.config)
        if request.evaluator_type == "MODEL_JUDGE":
            config.setdefault("prompt_version", "tracecore-judge-v1")
        version = (
            self.session.scalar(
                select(func.max(EvaluatorDefinition.version)).where(
                    EvaluatorDefinition.project_id == request.project_id,
                    EvaluatorDefinition.name == request.name,
                )
            )
            or 0
        ) + 1
        evaluator = EvaluatorDefinition(
            project_id=request.project_id,
            name=request.name,
            version=version,
            evaluator_type=request.evaluator_type,
            config=self.config_redactor.redact(config),
        )
        self.session.add(evaluator)
        self.session.commit()
        self.session.refresh(evaluator)
        return evaluator

    def run(self, request: EvaluationCreate) -> EvaluationRun:
        dataset = self.session.get(EvaluationDataset, request.dataset_id)
        if dataset is None:
            raise LookupError(f"Dataset `{request.dataset_id}` was not found")
        cases = self.session.scalars(
            select(EvaluationCase)
            .where(EvaluationCase.dataset_id == dataset.id)
            .order_by(EvaluationCase.ordinal)
        ).all()
        cases_by_id = {case.id: case for case in cases}
        mappings = {item.case_id: item.run_id for item in request.case_runs}
        unknown_cases = sorted(set(mappings) - set(cases_by_id))
        if unknown_cases:
            raise ValueError(f"Case `{unknown_cases[0]}` does not belong to dataset `{dataset.id}`")
        evaluator_ids = set(request.evaluator_ids)
        evaluators = self.session.scalars(
            select(EvaluatorDefinition).where(EvaluatorDefinition.id.in_(evaluator_ids))
        ).all()
        if len(evaluators) != len(evaluator_ids):
            found = {evaluator.id for evaluator in evaluators}
            raise LookupError(f"Evaluator `{next(iter(evaluator_ids - found))}` was not found")
        for evaluator in evaluators:
            if evaluator.project_id != dataset.project_id:
                raise ValueError(f"Evaluator `{evaluator.id}` belongs to another project")
        runs_by_id: dict[str, Run] = {}
        for run_id in mappings.values():
            run = self.session.get(Run, run_id)
            if run is None:
                raise LookupError(f"Run `{run_id}` was not found")
            if run.project_id != dataset.project_id:
                raise ValueError(f"Run `{run_id}` belongs to another project")
            runs_by_id[run_id] = run
        spans_by_trace: dict[str, list[Span]] = {}
        trace_ids = {run.trace_id for run in runs_by_id.values()}
        if trace_ids:
            spans = self.session.scalars(
                select(Span)
                .where(Span.trace_id.in_(trace_ids))
                .order_by(Span.trace_id, Span.started_at)
            ).all()
            for span in spans:
                spans_by_trace.setdefault(span.trace_id, []).append(span)
        run_records = EvaluationRun(
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            evaluator_snapshot=[
                {
                    "id": evaluator.id,
                    "name": evaluator.name,
                    "version": evaluator.version,
                    "type": evaluator.evaluator_type,
                    "config": evaluator.config,
                }
                for evaluator in sorted(evaluators, key=lambda item: item.id)
            ],
            status="RUNNING",
            total_results=len(cases) * len(evaluators),
            passed=0,
            failed=0,
            unavailable=0,
            assessed=0,
            created_at=utc_now(),
        )
        self.session.add(run_records)
        self.session.flush()
        results: list[EvaluationResult] = []
        for case in cases:
            mapped_run_id = mappings.get(case.id)
            run = runs_by_id.get(mapped_run_id) if mapped_run_id else None
            run_spans = spans_by_trace.get(run.trace_id, []) if run else []
            for evaluator in evaluators:
                if evaluator.evaluator_type == "MODEL_JUDGE":
                    assessment = self.model_engine.evaluate(evaluator, case, run)
                    status, reason, details = (
                        assessment.status,
                        assessment.reason,
                        assessment.details,
                    )
                else:
                    status, reason, details = self.engine.evaluate(evaluator, case, run, run_spans)
                results.append(
                    EvaluationResult(
                        evaluation_run_id=run_records.id,
                        case_id=case.id,
                        dataset_version=dataset.version,
                        run_id=mapped_run_id,
                        evaluator_id=evaluator.id,
                        evaluator_version=evaluator.version,
                        status=status,
                        reason=reason,
                        details=details,
                    )
                )
        self.session.add_all(results)
        run_records.passed = sum(result.status == "PASSED" for result in results)
        run_records.failed = sum(result.status == "FAILED" for result in results)
        run_records.unavailable = sum(result.status == "UNAVAILABLE" for result in results)
        run_records.assessed = sum(result.status == "ASSESSED" for result in results)
        run_records.status = (
            "FAIL"
            if run_records.failed
            else "WARN"
            if run_records.unavailable
            else "ASSESSED"
            if run_records.assessed
            else "PASS"
        )
        run_records.completed_at = utc_now()
        self.session.commit()
        self.session.refresh(run_records)
        return run_records
