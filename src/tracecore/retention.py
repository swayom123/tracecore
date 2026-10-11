"""Explicit retention and scoped, auditable run deletion."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tracecore.audit import append_audit
from tracecore.models import (
    AgentAnalysis,
    AlertDeliveryAttempt,
    AlertOccurrence,
    EvaluationResult,
    EvaluationRun,
    Experiment,
    ExperimentResult,
    ExperimentRun,
    PolicyEvaluation,
    PolicyTransition,
    PolicyViolation,
    Run,
    Span,
    Trace,
    TraceEvent,
)
from tracecore.security import Principal


def retention_days_by_environment(raw: str) -> dict[str, int]:
    try:
        parsed: Any = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("retention_days_by_environment must be a JSON object") from exc
    if not isinstance(parsed, dict) or any(
        not isinstance(env, str)
        or not env
        or isinstance(days, bool)
        or not isinstance(days, int)
        or days < 1
        for env, days in parsed.items()
    ):
        raise ValueError(
            "retention_days_by_environment must map environment names to positive day counts"
        )
    return parsed


def delete_runs_before(
    session: Session,
    principal: Principal,
    *,
    project_id: str,
    environment: str,
    before: datetime,
) -> dict[str, Any]:
    if before.tzinfo is None:
        raise ValueError("Deletion cutoff must include a timezone")
    runs = list(
        session.scalars(
            select(Run).where(
                Run.project_id == project_id,
                Run.environment == environment,
                Run.started_at < before,
            )
        )
    )
    run_ids = [run.id for run in runs]
    if not run_ids:
        append_audit(
            session,
            principal,
            action="retention.delete_runs",
            target_type="run_batch",
            target_id=None,
            project_id=project_id,
            details={
                "environment": environment,
                "cutoff": before.astimezone(UTC).isoformat(),
                "deleted_count": 0,
            },
        )
        return {"deleted_runs": 0, "run_ids": []}
    trace_ids = [run.trace_id for run in runs]
    violations = list(
        session.scalars(select(PolicyViolation).where(PolicyViolation.run_id.in_(run_ids)))
    )
    violation_ids = [item.id for item in violations]
    evaluation_run_ids = list(
        session.scalars(
            select(EvaluationResult.evaluation_run_id)
            .where(EvaluationResult.run_id.in_(run_ids))
            .distinct()
        )
    )
    experiment_run_ids = list(
        session.scalars(
            select(ExperimentResult.experiment_run_id)
            .where(ExperimentResult.run_id.in_(run_ids))
            .distinct()
        )
    )
    experiment_definitions = list(
        session.scalars(select(Experiment).where(Experiment.project_id == project_id))
    )
    changed_experiment_ids: list[str] = []
    mapping_count = 0
    for experiment in experiment_definitions:
        updated_candidates: list[dict[str, Any]] = []
        changed = False
        for candidate in experiment.candidates:
            item = dict(candidate)
            case_runs = item.get("case_runs")
            if isinstance(case_runs, dict):
                filtered = {
                    case_id: mapped_run_id
                    for case_id, mapped_run_id in case_runs.items()
                    if mapped_run_id not in run_ids
                }
                mapping_count += len(case_runs) - len(filtered)
                changed = changed or len(filtered) != len(case_runs)
                item["case_runs"] = filtered
            updated_candidates.append(item)
        if changed:
            experiment.candidates = updated_candidates
            changed_experiment_ids.append(experiment.id)
    if changed_experiment_ids:
        experiment_run_ids.extend(
            session.scalars(
                select(ExperimentRun.id).where(
                    ExperimentRun.experiment_id.in_(changed_experiment_ids)
                )
            )
        )
    experiment_run_ids = list(set(experiment_run_ids))
    previous_scope = session.info.get("skip_scope", False)
    session.info["skip_scope"] = True
    try:
        project_alerts = list(
            session.scalars(select(AlertOccurrence).where(AlertOccurrence.project_id == project_id))
        )
        expired_alert_ids = [
            alert.id
            for alert in project_alerts
            if alert.evidence.get("scope", {}).get("environment") in {None, environment}
        ]
        if expired_alert_ids:
            session.execute(
                delete(AlertDeliveryAttempt).where(
                    AlertDeliveryAttempt.occurrence_id.in_(expired_alert_ids)
                )
            )
            session.execute(
                delete(AlertOccurrence).where(AlertOccurrence.id.in_(expired_alert_ids))
            )
    finally:
        session.info["skip_scope"] = previous_scope
    # Purge per-run evaluation/experiment snapshots and their aggregate rows; retain definitions and audit history.
    for model, condition in (
        (
            PolicyTransition,
            PolicyTransition.violation_id.in_(violation_ids),
        ),
        (PolicyViolation, PolicyViolation.id.in_(violation_ids)),
        (PolicyEvaluation, PolicyEvaluation.run_id.in_(run_ids)),
        (AgentAnalysis, AgentAnalysis.run_id.in_(run_ids)),
        (EvaluationResult, EvaluationResult.run_id.in_(run_ids)),
        (EvaluationResult, EvaluationResult.evaluation_run_id.in_(evaluation_run_ids)),
        (EvaluationRun, EvaluationRun.id.in_(evaluation_run_ids)),
        (ExperimentResult, ExperimentResult.run_id.in_(run_ids)),
        (ExperimentResult, ExperimentResult.experiment_run_id.in_(experiment_run_ids)),
        (ExperimentRun, ExperimentRun.id.in_(experiment_run_ids)),
        (TraceEvent, TraceEvent.trace_id.in_(trace_ids)),
        (Span, Span.trace_id.in_(trace_ids)),
        (Trace, Trace.trace_id.in_(trace_ids)),
        (Run, Run.id.in_(run_ids)),
    ):
        session.execute(delete(model).where(condition))
    append_audit(
        session,
        principal,
        action="retention.delete_runs",
        target_type="run_batch",
        target_id=None,
        project_id=project_id,
        details={
            "environment": environment,
            "cutoff": before.astimezone(UTC).isoformat(),
            "deleted_count": len(run_ids),
            "experiment_mappings_removed": mapping_count,
            "alert_occurrences_removed": len(expired_alert_ids),
        },
    )
    return {"deleted_runs": len(run_ids), "run_ids": run_ids}


def retention_cutoff(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)
