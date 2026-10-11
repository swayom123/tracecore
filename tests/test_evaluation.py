from types import SimpleNamespace

import pytest

from tracecore.evaluation import (
    DeterministicEvaluationEngine,
    EvaluationConfigurationError,
    validate_evaluator_config,
)

engine = DeterministicEvaluationEngine()
case = SimpleNamespace(expected_output={"answer": "correct"}, expected_output_present=True)


def evaluator(kind: str, config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(evaluator_type=kind, config=config or {})


def run(
    output: object = None,
    status: str = "SUCCEEDED",
    duration: float | None = 40,
    cost: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        output_reference=output, status=status, duration_ms=duration, estimated_cost=cost
    )


def test_exact_match_and_required_fields_are_deterministic() -> None:
    assert (
        engine.evaluate(evaluator("EXACT_MATCH"), case, run({"answer": "correct"}), [])[0]
        == "PASSED"
    )
    assert (
        engine.evaluate(evaluator("EXACT_MATCH"), case, run({"answer": "wrong"}), [])[0] == "FAILED"
    )
    status, reason, details = engine.evaluate(
        evaluator("REQUIRED_FIELDS", {"fields": ["answer", "source"]}),
        case,
        run({"answer": "correct"}),
        [],
    )
    assert status == "FAILED"
    assert "source" in reason
    assert details["missing_fields"] == ["source"]
    omitted_expected = SimpleNamespace(expected_output=None, expected_output_present=False)
    assert (
        engine.evaluate(evaluator("EXACT_MATCH"), omitted_expected, run({"answer": "x"}), [])[0]
        == "UNAVAILABLE"
    )


def test_content_and_outcome_evaluators_use_only_recorded_values() -> None:
    assert engine.evaluate(evaluator("VALID_JSON"), case, run('{"answer": 1}'), [])[0] == "PASSED"
    assert engine.evaluate(evaluator("VALID_JSON"), case, run("{bad json"), [])[0] == "FAILED"
    assert (
        engine.evaluate(
            evaluator("REGEX_MATCH", {"pattern": r"answer=\d+"}), case, run("answer=42"), []
        )[0]
        == "PASSED"
    )
    assert (
        engine.evaluate(evaluator("RESPONSE_LENGTH", {"min_chars": 5}), case, run("tiny"), [])[0]
        == "FAILED"
    )
    assert (
        engine.evaluate(evaluator("CITATION_PRESENT"), case, run("Evidence [source-1]"), [])[0]
        == "PASSED"
    )
    assert engine.evaluate(evaluator("TASK_OUTCOME"), case, run(status="FAILED"), [])[0] == "FAILED"


def test_telemetry_evaluators_report_unavailable_instead_of_guessing() -> None:
    assert (
        engine.evaluate(evaluator("LATENCY_THRESHOLD", {"max_ms": 50}), case, run(duration=40), [])[
            0
        ]
        == "PASSED"
    )
    status, reason, _ = engine.evaluate(
        evaluator("COST_THRESHOLD", {"max_cost": 1}), case, run(cost=None), []
    )
    assert status == "UNAVAILABLE"
    assert "Cost unavailable" in reason
    status, _, _ = engine.evaluate(
        evaluator("LATENCY_THRESHOLD", {"max_ms": 50}), case, run(duration=None), []
    )
    assert status == "UNAVAILABLE"
    tools = [
        SimpleNamespace(span_type="TOOL", name="Search"),
        SimpleNamespace(span_type="LLM", name="model"),
    ]
    status, _, details = engine.evaluate(
        evaluator("TOOL_CALL_CONSTRAINTS", {"allowed_tools": ["Database"]}),
        case,
        run({"answer": "correct"}),
        tools,
    )
    assert status == "FAILED"
    assert details["disallowed"] == ["Search"]


def test_config_rejects_unknown_fields_and_invalid_regex() -> None:
    with pytest.raises(EvaluationConfigurationError, match="Unsupported config fields"):
        validate_evaluator_config("TASK_OUTCOME", {"threshold": 0.8})
    with pytest.raises(EvaluationConfigurationError, match="pattern is invalid"):
        validate_evaluator_config("REGEX_MATCH", {"pattern": "["})
    with pytest.raises(EvaluationConfigurationError, match="safe regex subset"):
        validate_evaluator_config("REGEX_MATCH", {"pattern": "(a+)+$"})
