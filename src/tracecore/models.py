from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tracecore.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    applications: Mapped[list["Application"]] = relationship(back_populates="project")


class ApiCredential(Base):
    """A revocable bearer credential; raw token material is never persisted."""

    __tablename__ = "api_credentials"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    environments: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateLimitBucket(Base):
    """Shared fixed-window counters used by every API process on the database."""

    __tablename__ = "rate_limit_buckets"
    __table_args__ = (Index("ix_rate_limit_buckets_window", "window_start"),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    project: Mapped[Project] = relationship(back_populates="applications")


class Environment(Base):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("application_id", "name"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("application_id", "agent_name", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    agent_name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    code_reference: Mapped[str | None] = mapped_column(String(500))
    model_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_project_started", "project_id", "started_at"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    environment: Mapped[str] = mapped_column(String(80), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(200))
    agent_version_id: Mapped[str | None] = mapped_column(ForeignKey("agent_versions.id"))
    workflow_name: Mapped[str | None] = mapped_column(String(200))
    trace_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    input_reference: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_reference: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    model_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class Trace(Base):
    __tablename__ = "traces"
    trace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True, index=True)
    root_span_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (Index("ix_spans_trace_start", "trace_id", "started_at"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trace_id: Mapped[str] = mapped_column(ForeignKey("traces.trace_id"), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(128), index=True)
    span_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_reference: Mapped[Any | None] = mapped_column(JSON)
    output_reference: Mapped[Any | None] = mapped_column(JSON)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class TraceEvent(Base):
    __tablename__ = "trace_events"
    __table_args__ = (Index("ix_events_trace_time", "trace_id", "timestamp"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(ForeignKey("traces.trace_id"), index=True)
    span_id: Mapped[str | None] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvaluationDataset(Base):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version"),
        Index("ix_eval_datasets_project_name", "project_id", "name"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (UniqueConstraint("dataset_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    input: Mapped[Any | None] = mapped_column(JSON)
    expected_output: Mapped[Any | None] = mapped_column(JSON)
    expected_output_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reference_context: Mapped[Any | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class EvaluatorDefinition(Base):
    __tablename__ = "evaluator_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version"),
        Index("ix_evaluators_project_name", "project_id", "name"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluator_type: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (Index("ix_evaluation_runs_created", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"), index=True)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluator_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    total_results: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, nullable=False)
    unavailable: Mapped[int] = mapped_column(Integer, nullable=False)
    assessed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        UniqueConstraint("evaluation_run_id", "case_id", "evaluator_id"),
        Index("ix_evaluation_results_run", "evaluation_run_id"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    evaluation_run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), index=True)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    evaluator_id: Mapped[str] = mapped_column(ForeignKey("evaluator_definitions.id"), index=True)
    evaluator_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Experiment(Base):
    """Immutable experiment definition version; candidates reference existing runs only."""

    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version"),
        Index("ix_experiments_project_name", "project_id", "name"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"), index=True)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    __table_args__ = (Index("ix_experiment_runs_created", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    experiment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"))
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluator_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    candidate_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    criteria_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperimentResult(Base):
    __tablename__ = "experiment_results"
    __table_args__ = (
        UniqueConstraint("experiment_run_id", "candidate_id", "case_id", "evaluator_id"),
        Index("ix_experiment_results_run", "experiment_run_id"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    experiment_run_id: Mapped[str] = mapped_column(ForeignKey("experiment_runs.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    evaluator_id: Mapped[str] = mapped_column(ForeignKey("evaluator_definitions.id"), index=True)
    evaluator_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentAnalysis(Base):
    __tablename__ = "agent_analyses"
    __table_args__ = (Index("ix_agent_analyses_run_created", "run_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GovernancePolicy(Base):
    __tablename__ = "governance_policies"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version"),
        Index("ix_governance_policies_project_name", "project_id", "name"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    effect: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PolicyEvaluation(Base):
    __tablename__ = "policy_evaluations"
    __table_args__ = (Index("ix_policy_evaluations_run_created", "run_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("governance_policies.id"), index=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PolicyViolation(Base):
    __tablename__ = "policy_violations"
    __table_args__ = (Index("ix_policy_violations_project_status", "project_id", "status"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    policy_evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("policy_evaluations.id"), index=True
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("governance_policies.id"), index=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    effect: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    waiver_actor: Mapped[str | None] = mapped_column(String(200))
    waiver_reason: Mapped[str | None] = mapped_column(Text)
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waiver_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PolicyTransition(Base):
    __tablename__ = "policy_transitions"
    __table_args__ = (
        Index("ix_policy_transitions_violation_created", "violation_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    violation_id: Mapped[str] = mapped_column(ForeignKey("policy_violations.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MonitoringBudget(Base):
    __tablename__ = "monitoring_budgets"
    __table_args__ = (Index("ix_monitoring_budgets_project", "project_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    limits: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MonitorAlertRule(Base):
    __tablename__ = "monitor_alert_rules"
    __table_args__ = (Index("ix_monitor_alert_rules_project", "project_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(50), nullable=False)
    operator: Mapped[str] = mapped_column(String(10), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    destination: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grouping_key: Mapped[str] = mapped_column(String(100), nullable=False, default="rule")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AlertOccurrence(Base):
    __tablename__ = "alert_occurrences"
    __table_args__ = (Index("ix_alert_occurrences_project_created", "project_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    alert_group_id: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("monitor_alert_rules.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertDeliveryAttempt(Base):
    __tablename__ = "alert_delivery_attempts"
    __table_args__ = (Index("ix_alert_delivery_occurrence", "occurrence_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("alert_occurrences.id"), index=True)
    destination_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    response_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(500))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_org_created", "organization_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
