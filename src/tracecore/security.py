"""Bearer authentication, role policy, and ORM tenant/environment scopes."""

import hashlib
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker, with_loader_criteria

from tracecore.config import Settings
from tracecore.models import (
    AgentAnalysis,
    AgentVersion,
    AlertDeliveryAttempt,
    AlertOccurrence,
    ApiCredential,
    Application,
    AuditEvent,
    Environment,
    EvaluationCase,
    EvaluationDataset,
    EvaluationResult,
    EvaluationRun,
    EvaluatorDefinition,
    Experiment,
    ExperimentResult,
    ExperimentRun,
    GovernancePolicy,
    MonitorAlertRule,
    MonitoringBudget,
    PolicyEvaluation,
    PolicyTransition,
    PolicyViolation,
    Project,
    RateLimitBucket,
    Run,
    Span,
    Trace,
    TraceEvent,
)

ROLES = {"ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER", "EVALUATOR", "AUDITOR", "VIEWER"}


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    organization_id: str | None = None
    project_ids: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    development: bool = False


def configured_principals(settings: Settings) -> dict[str, Principal] | None:
    if not settings.api_tokens:
        return None
    try:
        raw = json.loads(settings.api_tokens)
        if not isinstance(raw, dict) or not raw:
            return None
        principals: dict[str, Principal] = {}
        for token, value in raw.items():
            if not isinstance(token, str) or len(token) < 32 or not isinstance(value, dict):
                return None
            role = value.get("role")
            org = value.get("organization_id")
            projects = value.get("project_ids")
            environments = value.get("environments")
            subject = value.get("subject")
            if (
                role not in ROLES
                or not isinstance(org, str)
                or not org
                or not isinstance(projects, list)
                or not projects
                or not all(isinstance(item, str) and item for item in projects)
                or not isinstance(environments, list)
                or not environments
                or not all(isinstance(item, str) and item for item in environments)
            ):
                return None
            principals[token] = Principal(
                subject=subject if isinstance(subject, str) and subject else "configured-token",
                role=role,
                organization_id=org,
                project_ids=tuple(projects),
                environments=tuple(environments),
            )
        return principals
    except (ValueError, TypeError):
        return None


def authenticate(
    headers: dict[bytes, bytes],
    settings: Settings,
    session_factory: sessionmaker[Any] | None = None,
) -> tuple[Principal | None, str | None]:
    if settings.runtime_environment == "production" and settings.auth_mode != "bearer":
        return None, "unconfigured"
    if settings.auth_mode == "development":
        return Principal(
            "local-development", "ADMIN", organization_id="local-development", development=True
        ), None
    principals = configured_principals(settings)
    authorization = headers.get(b"authorization", b"").decode("latin-1")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None, "unconfigured" if principals is None else "unauthorized"
    if session_factory is not None:
        session = session_factory()
        session.info["skip_scope"] = True
        try:
            credential = session.scalar(
                select(ApiCredential).where(
                    ApiCredential.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                    ApiCredential.revoked_at.is_(None),
                )
            )
            if credential is not None:
                expires_at = credential.expires_at
                if expires_at is not None:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                    if expires_at <= datetime.now(UTC):
                        return None, "unauthorized"
                return (
                    Principal(
                        subject=credential.subject,
                        role=credential.role,
                        organization_id=credential.organization_id,
                        project_ids=tuple(credential.project_ids),
                        environments=tuple(credential.environments),
                    ),
                    None,
                )
        finally:
            session.close()
    if principals is not None:
        for known, principal in principals.items():
            if secrets.compare_digest(known, token):
                return principal, None
        return None, "unauthorized"
    return None, "unauthorized"


def role_allowed(role: str, method: str, path: str) -> bool:
    if role == "ADMIN":
        return True
    write_groups: dict[str, set[str]] = {
        "ingest": {"PLATFORM_ENGINEER", "AI_ENGINEER"},
        "evaluations": {"AI_ENGINEER", "EVALUATOR"},
        "experiments": {"AI_ENGINEER", "PLATFORM_ENGINEER"},
        "analytics": {"AI_ENGINEER", "PLATFORM_ENGINEER"},
        "governance": {"PLATFORM_ENGINEER"},
        "violations": {"PLATFORM_ENGINEER", "AUDITOR"},
        "monitoring": {"PLATFORM_ENGINEER"},
        "retention": set(),
    }
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        if path.startswith("/v1/traces"):
            return role in write_groups["ingest"]
        if path.startswith("/evaluations"):
            group = "evaluations"
            return role in write_groups[group]
        if path.startswith("/experiments"):
            return role in write_groups["experiments"]
        if path.startswith("/runs/") and path.endswith("/agent-analytics"):
            return role in write_groups["analytics"]
        if path.startswith("/runs/") and path.endswith("/policy-check"):
            return role in write_groups["governance"]
        if path.startswith("/policies"):
            return role in write_groups["governance"]
        if path.startswith("/violations"):
            return role in write_groups["violations"]
        if path.startswith("/monitoring"):
            return role in write_groups["monitoring"]
        if path.startswith("/projects/") and path.endswith("/retention/enforce"):
            return role == "ADMIN"
        if path.startswith("/projects/") and path.endswith("/runs"):
            return role == "ADMIN"
        if path.startswith("/retention"):
            return role == "ADMIN"
        if path.startswith("/audit"):
            return role in {"ADMIN", "AUDITOR"}
        return False
    if path.startswith("/audit"):
        return role in {"ADMIN", "AUDITOR"}
    if path.startswith("/monitoring"):
        return role in {"ADMIN", "PLATFORM_ENGINEER"}
    if path.startswith("/agent-analytics") or path.endswith("/agent-analytics"):
        return role in {"ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER"}
    if path.endswith(("/policy-check", "/policy-checks")):
        return role in {"ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER", "AUDITOR"}
    if path.startswith("/projects/") and (path.endswith(("/runs", "/retention/enforce"))):
        return role == "ADMIN"
    if path.startswith("/traces/") or (path.startswith("/runs/") and path.count("/") == 2):
        return role in {"ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER", "VIEWER"}
    if path.startswith(("/evaluations", "/experiments", "/experiment-runs")):
        return role in {"ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER", "EVALUATOR", "AUDITOR"}
    if path.startswith(("/policies", "/policy-evaluations", "/violations")):
        return role in {"ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER", "AUDITOR"}
    if path == "/runs":
        return role in ROLES
    return role in ROLES


class RequestRateLimiter:
    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_prune = 0

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        timestamp = now if now is not None else time.monotonic()
        with self._lock:
            values = self._requests[key]
            while values and timestamp - values[0] >= 60:
                values.popleft()
            if len(values) >= limit:
                return False
            values.append(timestamp)
            return True

    def allow_shared(
        self, key: str, limit: int, session_factory: sessionmaker[Any], now: float | None = None
    ) -> bool:
        """Atomically count a fixed 60-second window in the shared application database."""
        timestamp = int(now if now is not None else time.time())
        window = timestamp // 60 * 60
        digest = hashlib.sha256(key.encode()).hexdigest()
        session = session_factory()
        session.info["skip_scope"] = True
        try:
            values = {"key": digest, "window_start": window, "count": 1}
            dialect = session.bind.dialect.name if session.bind else ""
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(RateLimitBucket).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[RateLimitBucket.key, RateLimitBucket.window_start],
                set_={"count": RateLimitBucket.count + 1},
            )
            session.execute(statement)
            count = session.scalar(
                select(RateLimitBucket.count).where(
                    RateLimitBucket.key == digest, RateLimitBucket.window_start == window
                )
            )
            # Remove this key's expired rows and periodically prune revoked/inactive keys.
            session.query(RateLimitBucket).filter(
                RateLimitBucket.key == digest,
                RateLimitBucket.window_start < window - 120,
            ).delete(synchronize_session=False)
            should_prune_all = False
            with self._lock:
                should_prune_all = timestamp - self._last_prune >= 3600
            if should_prune_all:
                session.query(RateLimitBucket).filter(
                    RateLimitBucket.window_start < window - 120
                ).delete(synchronize_session=False)
            session.commit()
            if should_prune_all:
                with self._lock:
                    self._last_prune = timestamp
            return int(count or 0) <= limit
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def principal_rate_key(principal: Principal, token: str, client: str) -> str:
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16] if token else client
    return f"{principal.subject}:{fingerprint}"


def constrain_principal_projects(
    principal: Principal, session_factory: sessionmaker[Any]
) -> Principal:
    """Remove project IDs already owned by another organization; allow new ingest projects."""
    if principal.development or not principal.organization_id:
        return principal
    session = session_factory()
    session.info["skip_scope"] = True
    try:
        projects = session.scalars(
            select(Project).where(Project.id.in_(principal.project_ids))
        ).all()
        existing = {project.id: project.organization_id for project in projects}
    finally:
        session.close()
    allowed = tuple(
        project_id
        for project_id in principal.project_ids
        if project_id not in existing or existing[project_id] == principal.organization_id
    )
    return Principal(
        subject=principal.subject,
        role=principal.role,
        organization_id=principal.organization_id,
        project_ids=allowed,
        environments=principal.environments,
        development=principal.development,
    )


def install_orm_scope() -> None:
    direct_project_models = (
        Application,
        EvaluationDataset,
        EvaluatorDefinition,
        Experiment,
        AgentAnalysis,
        GovernancePolicy,
        PolicyEvaluation,
        PolicyViolation,
    )

    def belongs_to_scope(cls: Any, project_attr: Any, ids: tuple[str, ...]) -> Any:
        return project_attr.in_(ids)

    @event.listens_for(OrmSession, "do_orm_execute")
    def add_scope(execute_state: Any) -> None:
        session = execute_state.session
        principal: Principal | None = session.info.get("principal")
        if not principal or principal.development or session.info.get("skip_scope"):
            return
        ids = principal.project_ids
        org = principal.organization_id or ""
        environments = principal.environments
        statement = execute_state.statement
        statement = statement.options(
            with_loader_criteria(
                Project,
                lambda cls: cls.id.in_(ids) & (cls.organization_id == org),
                include_aliases=True,
            ),
            with_loader_criteria(
                Run,
                lambda cls: (
                    belongs_to_scope(cls, cls.project_id, ids) & cls.environment.in_(environments)
                ),
                include_aliases=True,
            ),
        )
        for model in direct_project_models:
            statement = statement.options(
                with_loader_criteria(
                    model,
                    lambda cls: belongs_to_scope(cls, cls.project_id, ids),
                    include_aliases=True,
                )
            )
        statement = statement.options(
            with_loader_criteria(
                MonitoringBudget,
                lambda cls: (
                    cls.project_id.in_(ids)
                    & (
                        cls.scope["environment"].as_string().in_(environments)
                        | (
                            cls.scope["environment"].as_string().is_(None)
                            & ~exists(
                                select(Run.__table__.c.id).where(
                                    Run.__table__.c.project_id == cls.project_id,
                                    Run.__table__.c.environment.not_in(environments),
                                )
                            )
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                MonitorAlertRule,
                lambda cls: (
                    cls.project_id.in_(ids)
                    & (
                        cls.scope["environment"].as_string().in_(environments)
                        | (
                            cls.scope["environment"].as_string().is_(None)
                            & ~exists(
                                select(Run.__table__.c.id).where(
                                    Run.__table__.c.project_id == cls.project_id,
                                    Run.__table__.c.environment.not_in(environments),
                                )
                            )
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                AlertOccurrence,
                lambda cls: (
                    cls.project_id.in_(ids)
                    & (
                        cls.evidence["scope"]["environment"].as_string().in_(environments)
                        | (
                            cls.evidence["scope"]["environment"].as_string().is_(None)
                            & ~exists(
                                select(Run.__table__.c.id).where(
                                    Run.__table__.c.project_id == cls.project_id,
                                    Run.__table__.c.environment.not_in(environments),
                                )
                            )
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                Trace,
                lambda cls: cls.trace_id.in_(
                    select(Run.trace_id).where(
                        Run.project_id.in_(ids), Run.environment.in_(environments)
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                Span,
                lambda cls: cls.trace_id.in_(
                    select(Run.trace_id).where(
                        Run.project_id.in_(ids), Run.environment.in_(environments)
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                TraceEvent,
                lambda cls: cls.trace_id.in_(
                    select(Run.trace_id).where(
                        Run.project_id.in_(ids), Run.environment.in_(environments)
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                AgentVersion,
                lambda cls: exists(
                    select(Application.id).where(
                        Application.id == cls.application_id, Application.project_id.in_(ids)
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                Environment,
                lambda cls: (
                    exists(
                        select(Application.id).where(
                            Application.id == cls.application_id, Application.project_id.in_(ids)
                        )
                    )
                    & cls.name.in_(environments)
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                EvaluationCase,
                lambda cls: exists(
                    select(EvaluationDataset.id).where(
                        EvaluationDataset.id == cls.dataset_id,
                        EvaluationDataset.project_id.in_(ids),
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                EvaluationRun,
                lambda cls: (
                    exists(
                        select(EvaluationDataset.id).where(
                            EvaluationDataset.id == cls.dataset_id,
                            EvaluationDataset.project_id.in_(ids),
                        )
                    )
                    & ~exists(
                        select(EvaluationResult.id).where(
                            EvaluationResult.evaluation_run_id == cls.id,
                            EvaluationResult.run_id.is_not(None),
                            ~exists(
                                select(Run.id).where(
                                    Run.id == EvaluationResult.run_id,
                                    belongs_to_scope(Run, Run.project_id, ids),
                                    Run.environment.in_(environments),
                                )
                            ),
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                EvaluationResult,
                lambda cls: (
                    exists(
                        select(EvaluationCase.id)
                        .join(EvaluationDataset, EvaluationDataset.id == EvaluationCase.dataset_id)
                        .where(
                            EvaluationCase.id == cls.case_id, EvaluationDataset.project_id.in_(ids)
                        )
                    )
                    & (
                        cls.run_id.is_(None)
                        | exists(
                            select(Run.id).where(
                                Run.id == cls.run_id,
                                belongs_to_scope(Run, Run.project_id, ids),
                                Run.environment.in_(environments),
                            )
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                ExperimentRun,
                lambda cls: (
                    exists(
                        select(Experiment.id).where(
                            Experiment.id == cls.experiment_id, Experiment.project_id.in_(ids)
                        )
                    )
                    & ~exists(
                        select(ExperimentResult.id).where(
                            ExperimentResult.experiment_run_id == cls.id,
                            ~exists(
                                select(Run.id).where(
                                    Run.id == ExperimentResult.run_id,
                                    belongs_to_scope(Run, Run.project_id, ids),
                                    Run.environment.in_(environments),
                                )
                            ),
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                ExperimentResult,
                lambda cls: (
                    exists(
                        select(ExperimentRun.id)
                        .join(Experiment, Experiment.id == ExperimentRun.experiment_id)
                        .where(
                            ExperimentRun.id == cls.experiment_run_id,
                            Experiment.project_id.in_(ids),
                        )
                    )
                    & exists(
                        select(Run.id).where(
                            Run.id == cls.run_id,
                            belongs_to_scope(Run, Run.project_id, ids),
                            Run.environment.in_(environments),
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                PolicyTransition,
                lambda cls: exists(
                    select(PolicyViolation.id).where(
                        PolicyViolation.id == cls.violation_id, PolicyViolation.project_id.in_(ids)
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                PolicyEvaluation,
                lambda cls: (
                    belongs_to_scope(cls, cls.project_id, ids)
                    & exists(
                        select(Run.id).where(
                            Run.id == cls.run_id,
                            belongs_to_scope(Run, Run.project_id, ids),
                            Run.environment.in_(environments),
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                PolicyViolation,
                lambda cls: (
                    belongs_to_scope(cls, cls.project_id, ids)
                    & exists(
                        select(Run.id).where(
                            Run.id == cls.run_id,
                            belongs_to_scope(Run, Run.project_id, ids),
                            Run.environment.in_(environments),
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                AgentAnalysis,
                lambda cls: (
                    belongs_to_scope(cls, cls.project_id, ids)
                    & exists(
                        select(Run.id).where(
                            Run.id == cls.run_id,
                            belongs_to_scope(Run, Run.project_id, ids),
                            Run.environment.in_(environments),
                        )
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                AlertDeliveryAttempt,
                lambda cls: exists(
                    select(AlertOccurrence.id).where(
                        AlertOccurrence.id == cls.occurrence_id, AlertOccurrence.project_id.in_(ids)
                    )
                ),
                include_aliases=True,
            ),
            with_loader_criteria(
                AuditEvent,
                lambda cls: (
                    (cls.organization_id == org)
                    & (cls.project_id.is_(None) | cls.project_id.in_(ids))
                    & (
                        cls.details["environment"].as_string().is_(None)
                        | cls.details["environment"].as_string().in_(environments)
                    )
                ),
                include_aliases=True,
            ),
        )
        execute_state.statement = statement

    @event.listens_for(OrmSession, "before_flush")
    def enforce_write_scope(session: OrmSession, _flush_context: Any, _instances: Any) -> None:
        principal: Principal | None = session.info.get("principal")
        if not principal or principal.development or session.info.get("skip_scope"):
            return
        for item in session.new.union(session.dirty):
            project_id = getattr(item, "project_id", None)
            if project_id is not None and project_id not in principal.project_ids:
                raise PermissionError("Project is outside the authenticated principal's scope")
            if isinstance(item, Project) and (
                item.id not in principal.project_ids
                or item.organization_id != principal.organization_id
            ):
                raise PermissionError(
                    "Project is outside the authenticated principal's organization"
                )
            if isinstance(item, Run) and item.environment not in principal.environments:
                raise PermissionError("Environment is outside the authenticated principal's scope")
            if isinstance(item, AuditEvent) and item.organization_id != principal.organization_id:
                raise PermissionError(
                    "Audit event organization does not match the authenticated principal"
                )


install_orm_scope()
