import argparse
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import select

from tracecore.agent_analytics import AgentAnalyticsService
from tracecore.agent_analytics_schemas import AgentAnalysisConfig, AgentAnalyticsFilters
from tracecore.audit import verify_audit
from tracecore.config import get_settings
from tracecore.database import SessionLocal
from tracecore.evaluation import EvaluationService
from tracecore.evaluation_schemas import DatasetCreate, EvaluationCreate, EvaluatorCreate
from tracecore.experiment_schemas import ExperimentCreate
from tracecore.experiments import ExperimentService
from tracecore.governance import GovernanceService
from tracecore.governance_schemas import PolicyCreate, ViolationTransitionRequest
from tracecore.models import (
    AlertDeliveryAttempt,
    EvaluationDataset,
    EvaluationResult,
    EvaluationRun,
    EvaluatorDefinition,
    Experiment,
    ExperimentResult,
    ExperimentRun,
    GovernancePolicy,
    MonitoringBudget,
    PolicyEvaluation,
    PolicyTransition,
    PolicyViolation,
    Project,
    Run,
    Span,
    Trace,
)
from tracecore.monitoring import MonitoringService
from tracecore.monitoring_schemas import MonitoringScope
from tracecore.retention import delete_runs_before, retention_cutoff, retention_days_by_environment
from tracecore.security import Principal


def _json(value: Any) -> str:
    return json.dumps(value, default=str, indent=2)


def _read_json(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise TypeError("JSON input must be an object")
    return value


def _case_run(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("use CASE_ID=RUN_ID")
    case_id, run_id = value.split("=", 1)
    if not case_id or not run_id:
        raise argparse.ArgumentTypeError("use non-empty CASE_ID=RUN_ID")
    return case_id, run_id


def main() -> None:
    parser = argparse.ArgumentParser(prog="tracecore")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    audit_parser = commands.add_parser("audit")
    audit_commands = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_verify = audit_commands.add_parser("verify")
    audit_verify.add_argument("--organization-id", required=True)
    audit_verify.add_argument("--expected-head-hash")
    audit_verify.add_argument("--expected-count", type=int)
    retention_parser = commands.add_parser("retention")
    retention_commands = retention_parser.add_subparsers(dest="retention_command", required=True)
    retention_enforce = retention_commands.add_parser("enforce")
    retention_enforce.add_argument("--project-id", required=True)
    retention_enforce.add_argument("--environment", action="append")
    run_parser = commands.add_parser("run")
    run_commands = run_parser.add_subparsers(dest="run_command", required=True)
    run_commands.add_parser("list")
    show_run = run_commands.add_parser("show")
    show_run.add_argument("run_id")
    trace_parser = commands.add_parser("trace")
    trace_commands = trace_parser.add_subparsers(dest="trace_command", required=True)
    show_trace = trace_commands.add_parser("show")
    show_trace.add_argument("trace_id")

    eval_parser = commands.add_parser("eval")
    eval_commands = eval_parser.add_subparsers(dest="eval_command", required=True)
    dataset_parser = eval_commands.add_parser("dataset")
    dataset_commands = dataset_parser.add_subparsers(dest="dataset_command", required=True)
    dataset_create = dataset_commands.add_parser("create")
    dataset_create.add_argument("--file", required=True)
    dataset_list = dataset_commands.add_parser("list")
    dataset_list.add_argument("--project-id")
    evaluator_parser = eval_commands.add_parser("evaluator")
    evaluator_commands = evaluator_parser.add_subparsers(dest="evaluator_command", required=True)
    evaluator_create = evaluator_commands.add_parser("create")
    evaluator_create.add_argument("--file", required=True)
    evaluator_list = evaluator_commands.add_parser("list")
    evaluator_list.add_argument("--project-id")
    eval_run = eval_commands.add_parser("run")
    eval_run.add_argument("--dataset-id", required=True)
    eval_run.add_argument("--evaluator-id", action="append", required=True)
    eval_run.add_argument("--case-run", action="append", type=_case_run, required=True)
    eval_show = eval_commands.add_parser("show")
    eval_show.add_argument("evaluation_id")

    experiment_parser = commands.add_parser("experiment")
    experiment_commands = experiment_parser.add_subparsers(dest="experiment_command", required=True)
    experiment_create = experiment_commands.add_parser("create")
    experiment_create.add_argument("--file", required=True)
    experiment_list = experiment_commands.add_parser("list")
    experiment_list.add_argument("--project-id")
    experiment_compare = experiment_commands.add_parser("compare")
    experiment_compare.add_argument("experiment_id")
    experiment_show = experiment_commands.add_parser("show")
    experiment_show.add_argument("experiment_run_id")

    agent_parser = commands.add_parser("agent")
    agent_commands = agent_parser.add_subparsers(dest="agent_command", required=True)
    analytics_parser = agent_commands.add_parser("analytics")
    analytics_commands = analytics_parser.add_subparsers(dest="analytics_command", required=True)
    analytics_run = analytics_commands.add_parser("run")
    analytics_run.add_argument("run_id")
    analytics_run.add_argument("--config")
    analytics_show = analytics_commands.add_parser("show")
    analytics_show.add_argument("run_id")
    analytics_aggregate = analytics_commands.add_parser("aggregate")
    analytics_aggregate.add_argument("--project-id", required=True)
    analytics_aggregate.add_argument("--application-id")
    analytics_aggregate.add_argument("--agent-name")
    analytics_aggregate.add_argument("--agent-version")
    analytics_aggregate.add_argument("--started-after")
    analytics_aggregate.add_argument("--started-before")

    policy_parser = commands.add_parser("policy")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_create = policy_commands.add_parser("create")
    policy_create.add_argument("--file", required=True)
    policy_list = policy_commands.add_parser("list")
    policy_list.add_argument("--project-id", required=True)
    policy_check = policy_commands.add_parser("check")
    policy_check.add_argument("run_id")
    policy_check.add_argument("--policy-id", action="append")
    policy_show = policy_commands.add_parser("show")
    policy_show.add_argument("evaluation_id")
    policy_violations = policy_commands.add_parser("violations")
    policy_violations.add_argument("--project-id", required=True)
    policy_violations.add_argument("--status")
    policy_transition = policy_commands.add_parser("transition")
    policy_transition.add_argument("violation_id")
    policy_transition.add_argument("--status", required=True)
    policy_transition.add_argument("--actor", required=True)
    policy_transition.add_argument("--reason", required=True)
    policy_transition.add_argument("--expires-at")
    policy_history = policy_commands.add_parser("history")
    policy_history.add_argument("violation_id")

    monitor = commands.add_parser("monitor")
    monitor_commands = monitor.add_subparsers(dest="monitor_command", required=True)
    monitor_metrics = monitor_commands.add_parser("metrics")
    monitor_metrics.add_argument("--project-id", required=True)
    monitor_metrics.add_argument("--environment")
    monitor_metrics.add_argument("--application-id")
    monitor_metrics.add_argument("--agent-name")
    monitor_metrics.add_argument("--agent-version")
    monitor_metrics.add_argument("--minimum-samples", type=int)
    budget_commands = monitor_commands.add_parser("budgets")
    budget_commands.add_argument("--project-id", required=True)
    rule_commands = monitor_commands.add_parser("rules")
    rule_commands.add_argument("--project-id", required=True)
    alert_commands = monitor_commands.add_parser("alerts")
    alert_commands.add_argument("--project-id", required=True)
    alert_commands.add_argument("--evaluate", action="store_true")
    alert_commands.add_argument("--rule-id")

    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("tracecore.api:app", host="127.0.0.1", port=8000)
        return
    with SessionLocal() as session:
        if args.command == "audit" and args.audit_command == "verify":
            result = verify_audit(session, args.organization_id)
            checkpoint_match = True
            if args.expected_head_hash is not None:
                checkpoint_match = checkpoint_match and (
                    result["head_hash"] == args.expected_head_hash
                )
            if args.expected_count is not None:
                checkpoint_match = checkpoint_match and result["checked"] == args.expected_count
            result["checkpoint_match"] = checkpoint_match
            print(_json(result))
            if not result["valid"] or not checkpoint_match:
                raise SystemExit(1)
            return
        if args.command == "retention" and args.retention_command == "enforce":
            try:
                policies = retention_days_by_environment(
                    get_settings().retention_days_by_environment
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
            project = session.get(Project, args.project_id)
            if project is None:
                print(f"Project `{args.project_id}` was not found", file=sys.stderr)
                raise SystemExit(2)
            if not project.organization_id:
                print(
                    "Project has no organization_id; retention audit cannot be attributed",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            environments = args.environment or sorted(policies)
            unknown = set(environments) - set(policies)
            if unknown:
                print(
                    f"No retention period configured for: {', '.join(sorted(unknown))}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            principal = Principal(
                subject="tracecore-retention-cli",
                role="ADMIN",
                organization_id=project.organization_id,
                project_ids=(project.id,),
                environments=tuple(environments),
            )
            retention_results = []
            for environment in environments:
                result = delete_runs_before(
                    session,
                    principal,
                    project_id=project.id,
                    environment=environment,
                    before=retention_cutoff(policies[environment]),
                )
                session.commit()
                retention_results.append(
                    {**result, "environment": environment, "retention_days": policies[environment]}
                )
            print(_json({"project_id": project.id, "results": retention_results}))
            return
        if args.command == "run" and args.run_command == "list":
            runs = session.scalars(select(Run).order_by(Run.started_at.desc()).limit(50)).all()
            print(
                _json(
                    [
                        {
                            "id": r.id,
                            "trace_id": r.trace_id,
                            "status": r.status,
                            "started_at": r.started_at,
                        }
                        for r in runs
                    ]
                )
            )
        elif args.command == "run" and args.run_command == "show":
            run = session.get(Run, args.run_id)
            if run is None:
                print(f"Run `{args.run_id}` was not found", file=sys.stderr)
                raise SystemExit(1)
            print(
                _json(
                    {
                        "id": run.id,
                        "trace_id": run.trace_id,
                        "status": run.status,
                        "environment": run.environment,
                    }
                )
            )
        elif args.command == "trace" and args.trace_command == "show":
            trace = session.get(Trace, args.trace_id)
            if trace is None:
                print(f"Trace `{args.trace_id}` was not found", file=sys.stderr)
                raise SystemExit(1)
            spans = session.scalars(
                select(Span).where(Span.trace_id == args.trace_id).order_by(Span.started_at)
            ).all()
            print(
                _json(
                    {
                        "trace_id": trace.trace_id,
                        "run_id": trace.run_id,
                        "root_span_id": trace.root_span_id,
                        "spans": [
                            {
                                "id": span.id,
                                "parent_span_id": span.parent_span_id,
                                "name": span.name,
                                "type": span.span_type,
                                "status": span.status,
                                "duration_ms": span.duration_ms,
                            }
                            for span in spans
                        ],
                    }
                )
            )
        elif args.command == "eval":
            try:
                if args.eval_command == "dataset" and args.dataset_command == "create":
                    dataset = EvaluationService(session).create_dataset(
                        DatasetCreate.model_validate(_read_json(args.file))
                    )
                    print(
                        _json(
                            {
                                "id": dataset.id,
                                "name": dataset.name,
                                "version": dataset.version,
                                "project_id": dataset.project_id,
                            }
                        )
                    )
                elif args.eval_command == "dataset" and args.dataset_command == "list":
                    dataset_query = select(EvaluationDataset)
                    if args.project_id:
                        dataset_query = dataset_query.where(
                            EvaluationDataset.project_id == args.project_id
                        )
                    datasets = session.scalars(
                        dataset_query.order_by(
                            EvaluationDataset.name, EvaluationDataset.version.desc()
                        )
                    ).all()
                    print(
                        _json(
                            [
                                {
                                    "id": item.id,
                                    "project_id": item.project_id,
                                    "name": item.name,
                                    "version": item.version,
                                    "created_at": item.created_at,
                                }
                                for item in datasets
                            ]
                        )
                    )
                elif args.eval_command == "evaluator" and args.evaluator_command == "create":
                    evaluator = EvaluationService(session).create_evaluator(
                        EvaluatorCreate.model_validate(_read_json(args.file))
                    )
                    print(
                        _json(
                            {
                                "id": evaluator.id,
                                "name": evaluator.name,
                                "version": evaluator.version,
                                "evaluator_type": evaluator.evaluator_type,
                                "project_id": evaluator.project_id,
                            }
                        )
                    )
                elif args.eval_command == "evaluator" and args.evaluator_command == "list":
                    evaluator_query = select(EvaluatorDefinition)
                    if args.project_id:
                        evaluator_query = evaluator_query.where(
                            EvaluatorDefinition.project_id == args.project_id
                        )
                    evaluators = session.scalars(
                        evaluator_query.order_by(
                            EvaluatorDefinition.name, EvaluatorDefinition.version.desc()
                        )
                    ).all()
                    print(
                        _json(
                            [
                                {
                                    "id": item.id,
                                    "project_id": item.project_id,
                                    "name": item.name,
                                    "version": item.version,
                                    "evaluator_type": item.evaluator_type,
                                    "config": item.config,
                                }
                                for item in evaluators
                            ]
                        )
                    )
                elif args.eval_command == "run":
                    request = EvaluationCreate.model_validate(
                        {
                            "dataset_id": args.dataset_id,
                            "evaluator_ids": args.evaluator_id,
                            "case_runs": [
                                {"case_id": case_id, "run_id": run_id}
                                for case_id, run_id in args.case_run
                            ],
                        }
                    )
                    completed_evaluation = EvaluationService(session).run(request)
                    print(
                        _json(
                            {
                                "id": completed_evaluation.id,
                                "status": completed_evaluation.status,
                                "dataset_version": completed_evaluation.dataset_version,
                                "passed": completed_evaluation.passed,
                                "failed": completed_evaluation.failed,
                                "unavailable": completed_evaluation.unavailable,
                                "assessed": completed_evaluation.assessed,
                            }
                        )
                    )
                elif args.eval_command == "show":
                    evaluation_detail = session.get(EvaluationRun, args.evaluation_id)
                    if evaluation_detail is None:
                        raise LookupError(f"Evaluation `{args.evaluation_id}` was not found")
                    results = session.scalars(
                        select(EvaluationResult).where(
                            EvaluationResult.evaluation_run_id == evaluation_detail.id
                        )
                    ).all()
                    print(
                        _json(
                            {
                                "id": evaluation_detail.id,
                                "status": evaluation_detail.status,
                                "dataset_id": evaluation_detail.dataset_id,
                                "dataset_version": evaluation_detail.dataset_version,
                                "evaluator_snapshot": evaluation_detail.evaluator_snapshot,
                                "passed": evaluation_detail.passed,
                                "failed": evaluation_detail.failed,
                                "unavailable": evaluation_detail.unavailable,
                                "assessed": evaluation_detail.assessed,
                                "results": [
                                    {
                                        "case_id": result.case_id,
                                        "dataset_version": result.dataset_version,
                                        "run_id": result.run_id,
                                        "evaluator_id": result.evaluator_id,
                                        "evaluator_version": result.evaluator_version,
                                        "status": result.status,
                                        "reason": result.reason,
                                        "details": result.details,
                                    }
                                    for result in results
                                ],
                            }
                        )
                    )
            except (TypeError, ValueError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
        elif args.command == "experiment":
            try:
                if args.experiment_command == "create":
                    experiment_item = ExperimentService(session).create(
                        ExperimentCreate.model_validate(_read_json(args.file))
                    )
                    print(
                        _json(
                            {
                                "id": experiment_item.id,
                                "name": experiment_item.name,
                                "version": experiment_item.version,
                                "project_id": experiment_item.project_id,
                                "dataset_version": experiment_item.dataset_version,
                            }
                        )
                    )
                elif args.experiment_command == "list":
                    query = select(Experiment)
                    if args.project_id:
                        query = query.where(Experiment.project_id == args.project_id)
                    items = session.scalars(
                        query.order_by(Experiment.name, Experiment.version.desc())
                    ).all()
                    print(
                        _json(
                            [
                                {
                                    "id": item.id,
                                    "name": item.name,
                                    "version": item.version,
                                    "project_id": item.project_id,
                                    "dataset_version": item.dataset_version,
                                    "baseline_candidate_id": item.baseline_candidate_id,
                                }
                                for item in items
                            ]
                        )
                    )
                elif args.experiment_command == "compare":
                    experiment_to_compare = session.get(Experiment, args.experiment_id)
                    if experiment_to_compare is None:
                        raise LookupError(f"Experiment `{args.experiment_id}` was not found")
                    comparison_item = ExperimentService(session).run(experiment_to_compare)
                    print(
                        _json(
                            {
                                "id": comparison_item.id,
                                "status": comparison_item.status,
                                "experiment_id": comparison_item.experiment_id,
                                "experiment_version": comparison_item.experiment_version,
                                "comparison": comparison_item.comparison,
                            }
                        )
                    )
                elif args.experiment_command == "show":
                    experiment_run_item = session.get(ExperimentRun, args.experiment_run_id)
                    if experiment_run_item is None:
                        raise LookupError(
                            f"Experiment run `{args.experiment_run_id}` was not found"
                        )
                    experiment_results = session.scalars(
                        select(ExperimentResult).where(
                            ExperimentResult.experiment_run_id == experiment_run_item.id
                        )
                    ).all()
                    print(
                        _json(
                            {
                                "id": experiment_run_item.id,
                                "experiment_id": experiment_run_item.experiment_id,
                                "experiment_version": experiment_run_item.experiment_version,
                                "status": experiment_run_item.status,
                                "comparison": experiment_run_item.comparison,
                                "results": [
                                    {
                                        "candidate_id": result.candidate_id,
                                        "case_id": result.case_id,
                                        "run_id": result.run_id,
                                        "evaluator_id": result.evaluator_id,
                                        "evaluator_version": result.evaluator_version,
                                        "status": result.status,
                                        "reason": result.reason,
                                        "details": result.details,
                                    }
                                    for result in experiment_results
                                ],
                            }
                        )
                    )
            except (TypeError, ValueError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
        elif args.command == "agent" and args.agent_command == "analytics":
            try:
                analytics_service = AgentAnalyticsService(session)
                if args.analytics_command == "run":
                    config = (
                        AgentAnalysisConfig.model_validate(_read_json(args.config))
                        if args.config
                        else AgentAnalysisConfig()
                    )
                    created_analysis = analytics_service.analyze(args.run_id, config)
                    print(
                        _json(
                            {
                                "id": created_analysis.id,
                                "run_id": created_analysis.run_id,
                                "created_at": created_analysis.created_at,
                                "config": created_analysis.config_snapshot,
                                "analysis": created_analysis.analysis,
                            }
                        )
                    )
                elif args.analytics_command == "show":
                    stored_analysis = analytics_service.latest(args.run_id)
                    if stored_analysis is None:
                        raise LookupError(
                            f"No agent analytics have been computed for run `{args.run_id}`"
                        )
                    print(
                        _json(
                            {
                                "id": stored_analysis.id,
                                "run_id": stored_analysis.run_id,
                                "created_at": stored_analysis.created_at,
                                "config": stored_analysis.config_snapshot,
                                "analysis": stored_analysis.analysis,
                            }
                        )
                    )
                elif args.analytics_command == "aggregate":
                    filters = AgentAnalyticsFilters(
                        project_id=args.project_id,
                        application_id=args.application_id,
                        agent_name=args.agent_name,
                        agent_version=args.agent_version,
                        started_after=args.started_after,
                        started_before=args.started_before,
                    )
                    print(_json(analytics_service.aggregate(filters)))
            except (TypeError, ValueError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
        elif args.command == "policy":
            try:
                governance = GovernanceService(session)
                if args.policy_command == "create":
                    created_policy = governance.create_policy(
                        PolicyCreate.model_validate(_read_json(args.file))
                    )
                    session.commit()
                    print(
                        _json(
                            {
                                "id": created_policy.id,
                                "project_id": created_policy.project_id,
                                "name": created_policy.name,
                                "version": created_policy.version,
                            }
                        )
                    )
                elif args.policy_command == "list":
                    policy_items = session.scalars(
                        select(GovernancePolicy)
                        .where(GovernancePolicy.project_id == args.project_id)
                        .order_by(GovernancePolicy.name, GovernancePolicy.version.desc())
                    ).all()
                    print(
                        _json(
                            [
                                {
                                    "id": item.id,
                                    "project_id": item.project_id,
                                    "name": item.name,
                                    "version": item.version,
                                    "conditions": item.conditions,
                                    "effect": item.effect,
                                    "severity": item.severity,
                                }
                                for item in policy_items
                            ]
                        )
                    )
                elif args.policy_command == "check":
                    checked = governance.evaluate(args.run_id, args.policy_id)
                    session.commit()
                    print(
                        _json(
                            [
                                {
                                    "id": item.id,
                                    "policy_id": item.policy_id,
                                    "policy_version": item.policy_version,
                                    "result": item.result,
                                    "decision": item.decision,
                                    "evidence": item.evidence,
                                    "violation_id": violation.id if violation else None,
                                }
                                for item, violation in checked
                            ]
                        )
                    )
                elif args.policy_command == "show":
                    evaluation_item = session.get(PolicyEvaluation, args.evaluation_id)
                    if evaluation_item is None:
                        raise LookupError(f"Policy evaluation `{args.evaluation_id}` was not found")
                    print(
                        _json(
                            {
                                "id": evaluation_item.id,
                                "run_id": evaluation_item.run_id,
                                "policy_id": evaluation_item.policy_id,
                                "policy_version": evaluation_item.policy_version,
                                "policy_snapshot": evaluation_item.policy_snapshot,
                                "result": evaluation_item.result,
                                "decision": evaluation_item.decision,
                                "evidence": evaluation_item.evidence,
                            }
                        )
                    )
                elif args.policy_command == "violations":
                    violation_query = select(PolicyViolation).where(
                        PolicyViolation.project_id == args.project_id
                    )
                    if args.status:
                        violation_query = violation_query.where(
                            PolicyViolation.status == args.status.upper()
                        )
                    violation_items = session.scalars(
                        violation_query.order_by(PolicyViolation.created_at.desc())
                    ).all()
                    print(
                        _json(
                            [
                                {
                                    "id": item.id,
                                    "run_id": item.run_id,
                                    "policy_id": item.policy_id,
                                    "policy_version": item.policy_version,
                                    "effect": item.effect,
                                    "severity": item.severity,
                                    "status": item.status,
                                    "evidence": item.evidence,
                                }
                                for item in violation_items
                            ]
                        )
                    )
                elif args.policy_command == "transition":
                    selected_violation = session.get(PolicyViolation, args.violation_id)
                    if selected_violation is None:
                        raise LookupError(f"Violation `{args.violation_id}` was not found")
                    body = {"status": args.status, "actor": args.actor, "reason": args.reason}
                    if args.expires_at:
                        body["expires_at"] = args.expires_at
                    governance.transition(
                        selected_violation, ViolationTransitionRequest.model_validate(body)
                    )
                    session.commit()
                    print(
                        _json(
                            {
                                "id": selected_violation.id,
                                "status": selected_violation.status,
                                "waiver_actor": selected_violation.waiver_actor,
                                "waiver_reason": selected_violation.waiver_reason,
                                "waived_at": selected_violation.waived_at,
                                "expires_at": selected_violation.waiver_expires_at,
                            }
                        )
                    )
                elif args.policy_command == "history":
                    if session.get(PolicyViolation, args.violation_id) is None:
                        raise LookupError(f"Violation `{args.violation_id}` was not found")
                    transitions = session.scalars(
                        select(PolicyTransition)
                        .where(PolicyTransition.violation_id == args.violation_id)
                        .order_by(PolicyTransition.created_at, PolicyTransition.id)
                    ).all()
                    print(
                        _json(
                            [
                                {
                                    "from_status": item.from_status,
                                    "to_status": item.to_status,
                                    "actor": item.actor,
                                    "reason": item.reason,
                                    "created_at": item.created_at,
                                    "expires_at": item.expires_at,
                                }
                                for item in transitions
                            ]
                        )
                    )
            except (TypeError, ValueError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
        elif args.command == "monitor":
            monitoring = MonitoringService(session)
            if args.monitor_command == "metrics":
                print(
                    _json(
                        monitoring.metrics(
                            args.project_id,
                            MonitoringScope(
                                environment=args.environment,
                                application_id=args.application_id,
                                agent_name=args.agent_name,
                                agent_version=args.agent_version,
                            ),
                            minimum_samples=args.minimum_samples,
                        )
                    )
                )
            elif args.monitor_command == "budgets":
                budget_items = session.scalars(
                    select(MonitoringBudget)
                    .where(MonitoringBudget.project_id == args.project_id)
                    .order_by(MonitoringBudget.created_at)
                ).all()
                print(
                    _json(
                        [
                            {"id": x.id, "name": x.name, "scope": x.scope, "limits": x.limits}
                            for x in budget_items
                        ]
                    )
                )
            elif args.monitor_command == "rules":
                rule_items = monitoring.rules(args.project_id)
                print(
                    _json(
                        [
                            {
                                "id": x.id,
                                "name": x.name,
                                "metric": x.metric,
                                "operator": x.operator,
                                "threshold": x.threshold,
                                "window_minutes": x.window_minutes,
                                "minimum_samples": x.minimum_samples,
                                "destination": {"type": x.destination.get("type")},
                            }
                            for x in rule_items
                        ]
                    )
                )
            elif args.monitor_command == "alerts":
                if args.evaluate:
                    alert_items = [
                        x for x in monitoring.evaluate_alerts(args.project_id, rule_id=args.rule_id)
                    ]
                else:
                    alert_items = monitoring.occurrences(args.project_id)
                print(
                    _json(
                        [
                            {
                                "id": x.id,
                                "rule_id": x.rule_id,
                                "fingerprint": x.fingerprint,
                                "evidence": x.evidence,
                                "status": x.status,
                                "suppressed": x.suppressed,
                                "deliveries": [
                                    {
                                        "destination": d.destination_type,
                                        "status": d.status,
                                        "attempted_at": d.attempted_at,
                                    }
                                    for d in session.scalars(
                                        select(AlertDeliveryAttempt).where(
                                            AlertDeliveryAttempt.occurrence_id == x.id
                                        )
                                    ).all()
                                ],
                            }
                            for x in alert_items
                        ]
                    )
                )


if __name__ == "__main__":
    main()
