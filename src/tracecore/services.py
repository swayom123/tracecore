import logging
from collections.abc import Iterable
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracecore.models import (
    AgentVersion,
    Application,
    Environment,
    EvaluationCase,
    EvaluatorDefinition,
    Project,
    Run,
    Span,
    Trace,
    TraceEvent,
)
from tracecore.schemas import TracePayload

logger = logging.getLogger("tracecore")


class TraceIngestor(Protocol):
    def ingest(self, payload: TracePayload) -> str: ...


class TraceRepository(Protocol):
    def get_run(self, run_id: str) -> Run | None: ...


class RedactionService(Protocol):
    def redact(self, value: Any) -> Any: ...


class CostCalculator(Protocol):
    def calculate(
        self, provider: str | None, model: str | None, usage: dict[str, Any]
    ) -> float | None: ...


class EvaluationEngine(Protocol):
    def evaluate(
        self,
        evaluator: EvaluatorDefinition,
        case: EvaluationCase,
        run: Run | None,
        spans: list[Span],
    ) -> tuple[str, str, dict[str, Any]]: ...


class PolicyEngine(Protocol):
    def check(self, run_id: str) -> list[dict[str, Any]]: ...


class AlertEngine(Protocol):
    def process(self, event: dict[str, Any]) -> None: ...


class ContentStore(Protocol):
    def put(self, key: str, content: Any) -> str: ...
    def get(self, reference: str) -> Any | None: ...


class NoCostCalculator:
    def calculate(self, provider: str | None, model: str | None, usage: dict[str, Any]) -> None:
        return None


class NoopEvaluationEngine:
    def evaluate(
        self,
        evaluator: EvaluatorDefinition,
        case: EvaluationCase,
        run: Run | None,
        spans: list[Span],
    ) -> tuple[str, str, dict[str, Any]]:
        return "UNAVAILABLE", "No evaluation engine was configured", {}


class NoopPolicyEngine:
    def check(self, run_id: str) -> list[dict[str, Any]]:
        return []


class NoopAlertEngine:
    def process(self, event: dict[str, Any]) -> None:
        return None


class SqlTraceRepository:
    def __init__(self, session: Session, redactor: RedactionService) -> None:
        self.session = session
        self.redactor = redactor

    def get_run(self, run_id: str) -> Run | None:
        return self.session.get(Run, run_id)

    def ingest(self, payload: TracePayload) -> str:
        run_data = payload.run
        existing = self.session.get(Run, run_data.run_id)
        if existing:
            if existing.trace_id == run_data.trace_id:
                logger.info(
                    "trace_ingest_deduplicated",
                    extra={"run_id": run_data.run_id, "trace_id": run_data.trace_id},
                )
                return run_data.run_id
            raise ValueError(f"run ID `{run_data.run_id}` already belongs to another trace")
        trace_existing = self.session.get(Trace, run_data.trace_id)
        if trace_existing:
            raise ValueError(f"trace ID `{run_data.trace_id}` already belongs to another run")

        principal = self.session.info.get("principal")
        previous_scope = self.session.info.get("skip_scope", False)
        self.session.info["skip_scope"] = True
        try:
            project = self.session.get(Project, run_data.project_id)
        finally:
            self.session.info["skip_scope"] = previous_scope
        if (
            project is not None
            and principal is not None
            and not principal.development
            and (
                project.id not in principal.project_ids
                or project.organization_id != principal.organization_id
            )
        ):
            raise PermissionError("Project is outside the authenticated principal's scope")
        if project is None:
            project = Project(
                id=run_data.project_id,
                name=run_data.project_name or run_data.project_id,
                organization_id=principal.organization_id
                if principal and not principal.development
                else None,
            )
            self.session.add(project)
            self.session.flush()
        application = self.session.get(Application, run_data.application_id)
        if application is None:
            application = Application(
                id=run_data.application_id,
                project_id=project.id,
                name=run_data.application_name or run_data.application_id,
            )
            self.session.add(application)
            self.session.flush()
        elif application.project_id != project.id:
            raise ValueError(
                f"application `{application.id}` does not belong to project `{project.id}`"
            )

        environment = self.session.scalar(
            select(Environment).where(
                Environment.application_id == application.id,
                Environment.name == run_data.environment,
            )
        )
        if environment is None:
            self.session.add(Environment(application_id=application.id, name=run_data.environment))
        agent_version_id = None
        if run_data.agent_name and run_data.agent_version:
            version = self.session.scalar(
                select(AgentVersion).where(
                    AgentVersion.application_id == application.id,
                    AgentVersion.agent_name == run_data.agent_name,
                    AgentVersion.version == run_data.agent_version,
                )
            )
            if version is None:
                version = AgentVersion(
                    application_id=application.id,
                    agent_name=run_data.agent_name,
                    version=run_data.agent_version,
                )
                self.session.add(version)
                self.session.flush()
            agent_version_id = version.id

        span_times = [span.started_at for span in payload.spans]
        end_times = [span.ended_at for span in payload.spans if span.ended_at]
        trace_started = min(span_times)
        trace_ended = max(end_times) if len(end_times) == len(payload.spans) else None
        run_end = run_data.completed_at
        duration_ms = (run_end - run_data.started_at).total_seconds() * 1000 if run_end else None
        trace_duration = (
            (trace_ended - trace_started).total_seconds() * 1000 if trace_ended else None
        )
        run = Run(
            id=run_data.run_id,
            project_id=project.id,
            application_id=application.id,
            environment=run_data.environment,
            agent_name=run_data.agent_name,
            agent_version_id=agent_version_id,
            workflow_name=run_data.workflow_name,
            trace_id=run_data.trace_id,
            input_reference=self.redactor.redact(run_data.input),
            output_reference=self.redactor.redact(run_data.output),
            status=run_data.status.value,
            started_at=run_data.started_at,
            completed_at=run_end,
            duration_ms=duration_ms,
            model_usage=self.redactor.redact(run_data.model_usage),
            token_usage=self.redactor.redact(run_data.token_usage),
            estimated_cost=None,
            tags=self.redactor.redact(run_data.tags),
            metadata_json=self.redactor.redact(run_data.metadata),
        )
        self.session.add(run)
        self.session.add(
            Trace(
                trace_id=run_data.trace_id,
                run_id=run_data.run_id,
                root_span_id=payload.root_span_id,
                started_at=trace_started,
                ended_at=trace_ended,
                duration_ms=trace_duration,
                status=run_data.status.value,
                attributes=self.redactor.redact(payload.attributes),
            )
        )
        # These tables are related only through FK columns, not ORM relationships. Flush their
        # rows before child spans so PostgreSQL's immediate foreign-key checks are satisfied.
        self.session.flush()
        for span in payload.spans:
            duration = (
                (span.ended_at - span.started_at).total_seconds() * 1000 if span.ended_at else None
            )
            self.session.add(
                Span(
                    id=span.span_id,
                    trace_id=run_data.trace_id,
                    parent_span_id=span.parent_span_id,
                    span_type=span.span_type.value,
                    name=span.name,
                    started_at=span.started_at,
                    ended_at=span.ended_at,
                    duration_ms=duration,
                    status=span.status.value,
                    input_reference=self.redactor.redact(span.input),
                    output_reference=self.redactor.redact(span.output),
                    attributes=self.redactor.redact(span.attributes),
                    error=self.redactor.redact(span.error),
                )
            )
            for event in span.events:
                self.session.add(
                    TraceEvent(
                        trace_id=run_data.trace_id,
                        span_id=span.span_id,
                        name=event.name,
                        timestamp=event.timestamp,
                        attributes=self.redactor.redact(event.attributes),
                    )
                )
        self.session.commit()
        logger.info(
            "trace_ingested", extra={"run_id": run_data.run_id, "trace_id": run_data.trace_id}
        )
        return run_data.run_id

    def list_spans(self, trace_id: str) -> Iterable[Span]:
        return self.session.scalars(
            select(Span).where(Span.trace_id == trace_id).order_by(Span.started_at)
        ).all()
