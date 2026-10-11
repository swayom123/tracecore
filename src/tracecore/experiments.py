"""Reproducible comparisons over explicitly mapped, already stored runs."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracecore.evaluation import DeterministicEvaluationEngine
from tracecore.experiment_schemas import ExperimentCreate
from tracecore.models import (
    EvaluationCase,
    EvaluationDataset,
    EvaluatorDefinition,
    Experiment,
    ExperimentResult,
    ExperimentRun,
    Run,
    Span,
    utc_now,
)
from tracecore.redaction import BasicRedactionService


class ExperimentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.engine = DeterministicEvaluationEngine()
        self.redactor = BasicRedactionService()

    def create(self, request: ExperimentCreate) -> Experiment:
        dataset = self.session.get(EvaluationDataset, request.dataset_id)
        if dataset is None:
            raise LookupError(f"Dataset `{request.dataset_id}` was not found")
        if dataset.project_id != request.project_id:
            raise ValueError("Dataset belongs to another project")
        cases = self.session.scalars(
            select(EvaluationCase).where(EvaluationCase.dataset_id == dataset.id)
        ).all()
        case_ids = {case.id for case in cases}
        if not case_ids:
            raise ValueError("Experiment dataset has no cases")
        for candidate in request.candidates:
            if set(candidate.case_runs) != case_ids:
                missing = sorted(case_ids - set(candidate.case_runs))
                extra = sorted(set(candidate.case_runs) - case_ids)
                raise ValueError(
                    f"Candidate `{candidate.id}` mappings must cover exactly the dataset cases; "
                    f"missing={missing}, unknown={extra}"
                )
        evaluator_ids = set(request.evaluator_ids)
        evaluators = self.session.scalars(
            select(EvaluatorDefinition).where(EvaluatorDefinition.id.in_(evaluator_ids))
        ).all()
        if len(evaluators) != len(evaluator_ids):
            found = {item.id for item in evaluators}
            raise LookupError(f"Evaluator `{next(iter(evaluator_ids - found))}` was not found")
        if any(item.project_id != dataset.project_id for item in evaluators):
            raise ValueError("All evaluators must belong to the experiment project")
        for evaluator in evaluators:
            self._validate_evaluator(evaluator)
        run_ids = {
            run_id for candidate in request.candidates for run_id in candidate.case_runs.values()
        }
        runs = self.session.scalars(select(Run).where(Run.id.in_(run_ids))).all()
        runs_by_id = {run.id: run for run in runs}
        if len(runs_by_id) != len(run_ids):
            raise LookupError(f"Run `{next(iter(run_ids - set(runs_by_id)))}` was not found")
        if any(run.project_id != dataset.project_id for run in runs):
            raise ValueError("All mapped runs must belong to the experiment project")
        version = (
            self.session.scalar(
                select(func.max(Experiment.version)).where(
                    Experiment.project_id == request.project_id,
                    Experiment.name == request.name,
                )
            )
            or 0
        )
        experiment = Experiment(
            project_id=request.project_id,
            name=request.name,
            version=version + 1,
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            baseline_candidate_id=request.baseline_candidate_id,
            evaluator_snapshot=self._evaluator_snapshots(evaluators),
            candidates=[
                {
                    "id": candidate.id,
                    "name": candidate.name,
                    "configuration": self.redactor.redact(candidate.configuration),
                    "case_runs": candidate.case_runs,
                }
                for candidate in request.candidates
            ],
            criteria=[criterion.model_dump() for criterion in request.criteria],
            created_at=utc_now(),
        )
        self.session.add(experiment)
        self.session.commit()
        self.session.refresh(experiment)
        return experiment

    def run(self, experiment: Experiment) -> ExperimentRun:
        dataset = self.session.get(EvaluationDataset, experiment.dataset_id)
        if dataset is None or dataset.version != experiment.dataset_version:
            raise LookupError("The experiment's pinned dataset version is unavailable")
        cases = self.session.scalars(
            select(EvaluationCase)
            .where(EvaluationCase.dataset_id == experiment.dataset_id)
            .order_by(EvaluationCase.ordinal)
        ).all()
        # Reconstruct evaluators from the immutable version snapshots.
        evaluators = [
            EvaluatorDefinition(
                id=item["id"],
                project_id=experiment.project_id,
                name=item["name"],
                version=item["version"],
                evaluator_type=item["type"],
                config=item["config"],
            )
            for item in experiment.evaluator_snapshot
        ]
        run_ids = {
            run_id
            for candidate in experiment.candidates
            for run_id in candidate["case_runs"].values()
        }
        runs_by_id = {
            run.id: run
            for run in self.session.scalars(select(Run).where(Run.id.in_(run_ids))).all()
        }
        missing_run_ids = run_ids - set(runs_by_id)
        if missing_run_ids:
            raise LookupError(f"Mapped run `{min(missing_run_ids)}` is no longer available")
        spans = self.session.scalars(
            select(Span).where(Span.trace_id.in_([run.trace_id for run in runs_by_id.values()]))
        ).all()
        spans_by_trace: dict[str, list[Span]] = {}
        for span in spans:
            spans_by_trace.setdefault(span.trace_id, []).append(span)
        evaluator_by_id = {item.id: item for item in evaluators}
        record = ExperimentRun(
            experiment_id=experiment.id,
            experiment_version=experiment.version,
            dataset_id=experiment.dataset_id,
            dataset_version=experiment.dataset_version,
            evaluator_snapshot=experiment.evaluator_snapshot,
            candidate_snapshot=experiment.candidates,
            criteria_snapshot=experiment.criteria,
            status="RUNNING",
            comparison={},
            created_at=utc_now(),
        )
        self.session.add(record)
        self.session.flush()
        result_rows: list[ExperimentResult] = []
        evaluated: dict[str, dict[str, list[str]]] = {}
        evaluator_breakdown: dict[str, dict[str, dict[str, int]]] = {}
        raw_metrics: dict[str, dict[str, Any]] = {}
        for candidate in experiment.candidates:
            candidate_id = candidate["id"]
            statuses: list[str] = []
            failures = 0
            latencies: list[float] = []
            costs: list[float] = []
            all_costs_recorded = True
            tool_calls = 0
            for case in cases:
                run_id = candidate["case_runs"][case.id]
                run = runs_by_id[run_id]
                run_spans = spans_by_trace.get(run.trace_id, [])
                statuses.append(run.status)
                failures += run.status == "FAILED"
                if run.duration_ms is not None:
                    latencies.append(run.duration_ms)
                if run.estimated_cost is None:
                    all_costs_recorded = False
                else:
                    costs.append(run.estimated_cost)
                tool_calls += sum(span.span_type == "TOOL" for span in run_spans)
                for snapshot in experiment.evaluator_snapshot:
                    evaluator = evaluator_by_id[snapshot["id"]]
                    status, reason, details = self.engine.evaluate(evaluator, case, run, run_spans)
                    result_rows.append(
                        ExperimentResult(
                            experiment_run_id=record.id,
                            candidate_id=candidate_id,
                            case_id=case.id,
                            run_id=run.id,
                            evaluator_id=evaluator.id,
                            evaluator_version=snapshot["version"],
                            status=status,
                            reason=reason,
                            details=details,
                        )
                    )
                    evaluated.setdefault(candidate_id, {}).setdefault(status, []).append(case.id)
                    per_evaluator = evaluator_breakdown.setdefault(candidate_id, {}).setdefault(
                        evaluator.id, {"PASSED": 0, "FAILED": 0, "UNAVAILABLE": 0}
                    )
                    per_evaluator[status] += 1
            raw_metrics[candidate_id] = {
                "evaluation_pass_rate": self._pass_rate(evaluated.get(candidate_id, {})),
                "failure_count": failures,
                "latency_ms": sum(latencies) / len(latencies)
                if len(latencies) == len(cases)
                else None,
                "recorded_cost": sum(costs) if all_costs_recorded else None,
                "tool_calls": tool_calls,
                "evaluator_breakdown": evaluator_breakdown.get(candidate_id, {}),
            }
        self.session.add_all(result_rows)
        record.comparison = self._compare(
            experiment.candidates,
            experiment.criteria,
            raw_metrics,
            request_baseline=experiment.baseline_candidate_id,
        )
        record.status = record.comparison["status"]
        record.completed_at = utc_now()
        self.session.commit()
        self.session.refresh(record)
        return record

    @staticmethod
    def _validate_evaluator(evaluator: EvaluatorDefinition) -> None:
        from tracecore.evaluation import validate_evaluator_config

        if evaluator.evaluator_type == "MODEL_JUDGE":
            raise ValueError(
                "Model-based evaluators are not supported in experiment comparisons in this phase"
            )
        validate_evaluator_config(evaluator.evaluator_type, evaluator.config)

    @staticmethod
    def _evaluator_snapshots(evaluators: Sequence[EvaluatorDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "name": item.name,
                "version": item.version,
                "type": item.evaluator_type,
                "config": item.config,
            }
            for item in sorted(evaluators, key=lambda item: item.id)
        ]

    @staticmethod
    def _pass_rate(statuses: dict[str, list[str]]) -> float | None:
        passed, failed = len(statuses.get("PASSED", [])), len(statuses.get("FAILED", []))
        assessed = passed + failed
        return passed / assessed if assessed else None

    @classmethod
    def _compare(
        cls,
        candidates: list[dict[str, Any]],
        criteria: list[dict[str, Any]],
        metrics: dict[str, dict[str, Any]],
        request_baseline: str | None,
    ) -> dict[str, Any]:
        # Candidate order is stable; baseline id is stored as an explicit candidate flag.
        baseline = request_baseline or next(
            (candidate["id"] for candidate in candidates if candidate.get("is_baseline")),
            candidates[0]["id"],
        )
        baseline_metrics = metrics[baseline]
        outcomes: list[str] = []
        comparisons: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate["id"] == baseline:
                continue
            deltas: dict[str, Any] = {}
            candidate_outcomes: list[str] = []
            for criterion in criteria:
                metric = criterion["metric"]
                base_value, candidate_value = (
                    baseline_metrics[metric],
                    metrics[candidate["id"]][metric],
                )
                if base_value is None or candidate_value is None:
                    deltas[metric] = {
                        "baseline": base_value,
                        "candidate": candidate_value,
                        "delta": None,
                        "status": "UNAVAILABLE",
                        "reason": f"Metric {metric} unavailable: required data was not recorded for all mapped runs",
                    }
                    candidate_outcomes.append("WARN")
                    continue
                delta = candidate_value - base_value
                regression = delta if criterion["direction"] == "LOWER" else -delta
                state = (
                    "PASS" if regression <= criterion["max_regression"] else criterion["severity"]
                )
                candidate_outcomes.append(state)
                deltas[metric] = {
                    "baseline": base_value,
                    "candidate": candidate_value,
                    "delta": delta,
                    "regression": regression,
                    "max_regression": criterion["max_regression"],
                    "status": state,
                }
            status = (
                "FAIL"
                if "FAIL" in candidate_outcomes
                else "WARN"
                if "WARN" in candidate_outcomes
                else "PASS"
            )
            outcomes.append(status)
            comparisons.append(
                {
                    "candidate_id": candidate["id"],
                    "candidate_name": candidate["name"],
                    "status": status,
                    "metrics": metrics[candidate["id"]],
                    "evaluator_breakdown": metrics[candidate["id"]]["evaluator_breakdown"],
                    "criteria": deltas,
                }
            )
        overall = "FAIL" if "FAIL" in outcomes else "WARN" if "WARN" in outcomes else "PASS"
        return {
            "status": overall,
            "baseline_candidate_id": baseline,
            "baseline_candidate_name": next(
                item["name"] for item in candidates if item["id"] == baseline
            ),
            "candidates": [
                {
                    "candidate_id": baseline,
                    "candidate_name": next(
                        item["name"] for item in candidates if item["id"] == baseline
                    ),
                    "status": "BASELINE",
                    "metrics": baseline_metrics,
                    "evaluator_breakdown": baseline_metrics["evaluator_breakdown"],
                    "criteria": {},
                }
            ]
            + comparisons,
            "winner": None,
        }
