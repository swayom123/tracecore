import hashlib
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tracecore.agent_analytics import AgentAnalyticsService
from tracecore.agent_analytics_schemas import (
    AgentAnalyticsFilters,
    AgentAnalyticsRequest,
)
from tracecore.audit import append_audit, verify_audit
from tracecore.config import get_settings
from tracecore.database import SessionLocal, get_session
from tracecore.evaluation import EvaluationConfigurationError, EvaluationService
from tracecore.evaluation_schemas import DatasetCreate, EvaluationCreate, EvaluatorCreate
from tracecore.experiment_schemas import ExperimentCreate
from tracecore.experiments import ExperimentService
from tracecore.governance import GovernanceService
from tracecore.governance_schemas import (
    PolicyCheckRequest,
    PolicyCreate,
    ViolationTransitionRequest,
)
from tracecore.hardening_schemas import CredentialCreate
from tracecore.models import (
    AgentAnalysis,
    AgentVersion,
    AlertDeliveryAttempt,
    AlertOccurrence,
    ApiCredential,
    AuditEvent,
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
    Run,
    Span,
    Trace,
    TraceEvent,
)
from tracecore.monitoring import MonitoringService
from tracecore.monitoring_schemas import AlertRuleCreate, BudgetCreate, MonitoringScope
from tracecore.redaction import BasicRedactionService, MetadataOnlyRedactionService
from tracecore.retention import delete_runs_before, retention_cutoff, retention_days_by_environment
from tracecore.schemas import IngestResponse, TracePayload
from tracecore.security import (
    RequestRateLimiter,
    authenticate,
    constrain_principal_projects,
    principal_rate_key,
    role_allowed,
)
from tracecore.services import SqlTraceRepository

rate_limiter = RequestRateLimiter()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "trace_id",
            "run_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
        ):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(get_settings().log_level.upper())
    # HTTPX request logs may include sensitive webhook URL paths.
    logging.getLogger("httpx").setLevel(logging.WARNING)


app = FastAPI(title="TraceCore API", version="0.1.0")
configure_logging()
logger = logging.getLogger("tracecore.http")


class RequestContextMiddleware:
    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        supplied_id = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = (supplied_id or str(uuid4()))[:128]
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500

        async def send_with_context(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                message["headers"].append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        path = scope.get("path", "")
        if path != "/health":
            try:
                principal, auth_error = authenticate(headers, get_settings(), SessionLocal)
            except Exception:  # noqa: BLE001 - credential store failures must fail closed.
                await send_with_context(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send_with_context(
                    {
                        "type": "http.response.body",
                        "body": json.dumps(
                            {
                                "detail": "Authentication service is unavailable",
                                "request_id": request_id,
                            }
                        ).encode(),
                    }
                )
                return
            if auth_error is not None:
                client = scope.get("client")
                client_host = str(client[0]) if client else "unknown"
                try:
                    allowed = rate_limiter.allow_shared(
                        f"unauthenticated:{client_host}",
                        get_settings().rate_limit_per_minute,
                        SessionLocal,
                    )
                except Exception:  # noqa: BLE001 - fail closed if shared limiter storage fails.
                    allowed = False
                status = 429 if not allowed else (503 if auth_error == "unconfigured" else 401)
                detail = (
                    "Rate limit exceeded"
                    if status == 429
                    else (
                        "Authentication is not configured"
                        if status == 503
                        else "Bearer authentication is required"
                    )
                )
                await send_with_context(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(b"content-type", b"application/json")]
                        + ([(b"retry-after", b"60")] if status == 429 else []),
                    }
                )
                await send_with_context(
                    {
                        "type": "http.response.body",
                        "body": json.dumps({"detail": detail, "request_id": request_id}).encode(),
                    }
                )
                return
            assert principal is not None
            try:
                principal = constrain_principal_projects(principal, SessionLocal)
            except Exception:  # noqa: BLE001 - never serve a scoped request if ownership cannot be checked.
                await send_with_context(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send_with_context(
                    {
                        "type": "http.response.body",
                        "body": json.dumps(
                            {
                                "detail": "Organization scope could not be verified",
                                "request_id": request_id,
                            }
                        ).encode(),
                    }
                )
                return
            scope["state"]["principal"] = principal
            client = scope.get("client")
            client_host = str(client[0]) if client else "unknown"
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            token = authorization.partition(" ")[2]
            key = principal_rate_key(principal, token, client_host)
            try:
                allowed = rate_limiter.allow_shared(
                    key, get_settings().rate_limit_per_minute, SessionLocal
                )
            except Exception:  # noqa: BLE001 - fail closed if shared limiter storage fails.
                allowed = False
            if not allowed:
                await send_with_context(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", b"60"),
                        ],
                    }
                )
                await send_with_context(
                    {
                        "type": "http.response.body",
                        "body": json.dumps(
                            {"detail": "Rate limit exceeded", "request_id": request_id}
                        ).encode(),
                    }
                )
                return
            if not role_allowed(principal.role, scope["method"], path):
                await send_with_context(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send_with_context(
                    {
                        "type": "http.response.body",
                        "body": json.dumps(
                            {
                                "detail": "Role is not permitted to perform this action",
                                "request_id": request_id,
                            }
                        ).encode(),
                    }
                )
                return
        start = time.perf_counter()
        content_length = headers.get(b"content-length", b"")
        is_limited_payload = scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if (
            is_limited_payload
            and content_length.isdigit()
            and int(content_length) > get_settings().max_payload_bytes
        ):
            status_code = 413
            await send_with_context(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send_with_context(
                {
                    "type": "http.response.body",
                    "body": b'{"detail":"Payload exceeds configured size limit"}',
                }
            )
            return

        buffered_messages: list[dict[str, Any]] = []
        if is_limited_payload:
            received_bytes = 0
            while True:
                message = cast(dict[str, Any], await receive())
                if message["type"] == "http.request":
                    received_bytes += len(message.get("body", b""))
                    if received_bytes > get_settings().max_payload_bytes:
                        status_code = 413
                        await send_with_context(
                            {
                                "type": "http.response.start",
                                "status": 413,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        await send_with_context(
                            {
                                "type": "http.response.body",
                                "body": b'{"detail":"Payload exceeds configured size limit"}',
                            }
                        )
                        return
                buffered_messages.append(message)
                if message["type"] != "http.request" or not message.get("more_body", False):
                    break
        message_index = 0

        async def buffered_receive() -> dict[str, Any]:
            nonlocal message_index
            if message_index < len(buffered_messages):
                message = buffered_messages[message_index]
                message_index += 1
                return message
            return cast(dict[str, Any], await receive())

        try:
            await self.application(scope, buffered_receive, send_with_context)
        except Exception:  # noqa: BLE001 - convert unhandled API failures into a safe 500 response.
            logger.error(
                "request_failed",
                extra={"request_id": request_id, "method": scope["method"], "path": scope["path"]},
            )
            status_code = 500
            response_body = json.dumps(
                {"detail": "Internal server error", "request_id": request_id}, separators=(",", ":")
            ).encode()
            await send_with_context(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send_with_context({"type": "http.response.body", "body": response_body})
        logger.info(
            "request_complete",
            extra={
                "request_id": request_id,
                "method": scope["method"],
                "path": scope["path"],
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 3),
            },
        )
        principal = scope.get("state", {}).get("principal")
        if (
            principal is not None
            and scope["method"] == "GET"
            and status_code < 400
            and (
                path == "/runs"
                or (path.startswith("/runs/") and path.count("/") == 2)
                or path.startswith("/traces/")
            )
        ):
            try:
                audit_session = SessionLocal()
                audit_session.info["principal"] = principal
                access_targets: set[tuple[str, str]] = set()
                if path == "/runs":
                    query_values = parse_qs(scope.get("query_string", b"").decode("latin-1"))
                    requested_environment = query_values.get("environment", [None])[0]
                    environments = (
                        [requested_environment]
                        if requested_environment in principal.environments
                        else ([] if requested_environment else principal.environments)
                    )
                    access_targets.update(
                        (project_id, environment)
                        for project_id in principal.project_ids
                        for environment in environments
                    )
                else:
                    path_parts = path.split("/")
                    target_id = path_parts[2] if path.startswith("/traces/") else path_parts[-1]
                    run = audit_session.get(Run, target_id)
                    if run is None:
                        trace = audit_session.get(Trace, target_id)
                        run = audit_session.get(Run, trace.run_id) if trace else None
                    if run is not None:
                        access_targets.add((run.project_id, run.environment))
                for accessed_project, accessed_environment in sorted(access_targets):
                    append_audit(
                        audit_session,
                        principal,
                        action="trace.content_access",
                        target_type="trace_query" if path.startswith("/traces/") else "run_query",
                        target_id=(
                            path.split("/")[2]
                            if path.startswith("/traces/")
                            else path.split("/")[-1]
                        )
                        if path != "/runs"
                        else None,
                        project_id=accessed_project,
                        details={
                            "method": "GET",
                            "path": path,
                            "status_code": status_code,
                            "environment": accessed_environment,
                        },
                    )
                audit_session.commit()
            except Exception:  # noqa: BLE001 - access logs must not leak content or break reads.
                if "audit_session" in locals():
                    audit_session.rollback()
                logger.error("sensitive_access_audit_failed", extra={"request_id": request_id})
            finally:
                if "audit_session" in locals():
                    audit_session.close()


app.add_middleware(RequestContextMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {key: value for key, value in error.items() if key in {"loc", "msg", "type"}}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": errors, "request_id": getattr(request.state, "request_id", None)},
    )


def _redactor() -> BasicRedactionService | MetadataOnlyRedactionService:
    mode = get_settings().logging_mode
    return (
        MetadataOnlyRedactionService()
        if mode == "METADATA_ONLY"
        else BasicRedactionService(get_settings().redaction_patterns)
    )


def _run_json(run: Run, session: Session | None = None) -> dict[str, Any]:
    version = (
        session.get(AgentVersion, run.agent_version_id)
        if session and run.agent_version_id
        else None
    )
    return {
        "id": run.id,
        "project_id": run.project_id,
        "application_id": run.application_id,
        "environment": run.environment,
        "agent_name": run.agent_name,
        "agent_version_id": run.agent_version_id,
        "agent_version": version.version if version else None,
        "workflow_name": run.workflow_name,
        "trace_id": run.trace_id,
        "status": run.status,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "duration_ms": run.duration_ms,
        "token_usage": run.token_usage,
        "estimated_cost": "Cost unavailable" if run.estimated_cost is None else run.estimated_cost,
        "tags": run.tags,
        "metadata": run.metadata_json,
        "input": run.input_reference,
        "output": run.output_reference,
    }


def _span_json(span: Span) -> dict[str, Any]:
    return {
        "id": span.id,
        "trace_id": span.trace_id,
        "parent_span_id": span.parent_span_id,
        "span_type": span.span_type,
        "name": span.name,
        "started_at": span.started_at,
        "ended_at": span.ended_at,
        "duration_ms": span.duration_ms,
        "status": span.status,
        "input": span.input_reference,
        "output": span.output_reference,
        "attributes": span.attributes,
        "error": span.error,
    }


def _ingest(payload: TracePayload, session: Session) -> str:
    repository = SqlTraceRepository(session, _redactor())
    try:
        return repository.ingest(payload)
    except IntegrityError as exc:
        session.rollback()
        raise ValueError(
            "Trace conflicts with an existing run, trace, or application record"
        ) from exc
    except Exception:
        session.rollback()
        raise


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "data_mode": get_settings().data_mode}


def _credential_view(item: ApiCredential) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "subject": item.subject,
        "role": item.role,
        "organization_id": item.organization_id,
        "project_ids": item.project_ids,
        "environments": item.environments,
        "created_at": item.created_at,
        "expires_at": item.expires_at,
        "revoked_at": item.revoked_at,
    }


@app.post("/auth/tokens", status_code=201)
async def create_api_token(
    request: CredentialCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    principal = session.info.get("principal")
    if principal is None or principal.role != "ADMIN":
        raise HTTPException(status_code=403, detail="ADMIN role is required")
    if request.organization_id != principal.organization_id:
        raise HTTPException(
            status_code=403, detail="Credential organization is outside caller scope"
        )
    if request.expires_at is not None and request.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    if not set(request.project_ids).issubset(principal.project_ids):
        raise HTTPException(status_code=403, detail="Credential projects exceed caller scope")
    if not set(request.environments).issubset(principal.environments):
        raise HTTPException(status_code=403, detail="Credential environments exceed caller scope")
    token = "tc_" + secrets.token_urlsafe(36)
    item = ApiCredential(
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        name=request.name,
        subject=request.subject,
        role=request.role,
        organization_id=request.organization_id,
        project_ids=request.project_ids,
        environments=request.environments,
        expires_at=request.expires_at,
    )
    session.add(item)
    append_audit(
        session,
        principal,
        action="auth.credential.create",
        target_type="api_credential",
        target_id=item.id,
        project_id=request.project_ids[0] if len(request.project_ids) == 1 else None,
        details={"name": request.name, "role": request.role, "expires_at": request.expires_at},
    )
    session.commit()
    return {**_credential_view(item), "token": token}


@app.get("/auth/tokens")
async def list_api_tokens(session: Session = Depends(get_session)) -> dict[str, Any]:
    principal = session.info.get("principal")
    if principal is None or principal.role != "ADMIN":
        raise HTTPException(status_code=403, detail="ADMIN role is required")
    items = session.scalars(
        select(ApiCredential)
        .where(ApiCredential.organization_id == principal.organization_id)
        .order_by(ApiCredential.created_at.desc())
    ).all()
    return {"tokens": [_credential_view(item) for item in items]}


@app.delete("/auth/tokens/{credential_id}")
async def revoke_api_token(
    credential_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    principal = session.info.get("principal")
    if principal is None or principal.role != "ADMIN":
        raise HTTPException(status_code=403, detail="ADMIN role is required")
    item = session.scalar(
        select(ApiCredential).where(
            ApiCredential.id == credential_id,
            ApiCredential.organization_id == principal.organization_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Credential was not found")
    if item.revoked_at is None:
        item.revoked_at = datetime.now(UTC)
        append_audit(
            session,
            principal,
            action="auth.credential.revoke",
            target_type="api_credential",
            target_id=item.id,
            project_id=item.project_ids[0] if len(item.project_ids) == 1 else None,
            details={"name": item.name, "role": item.role},
        )
        session.commit()
    return _credential_view(item)


def _monitoring_service(session: Session) -> MonitoringService:
    return MonitoringService(session, get_settings().monitoring_percentile_min_samples)


def _alert_rule_json(item: MonitorAlertRule) -> dict[str, Any]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "name": item.name,
        "metric": item.metric,
        "operator": item.operator,
        "threshold": item.threshold,
        "window_minutes": item.window_minutes,
        "minimum_samples": item.minimum_samples,
        "severity": item.severity,
        "scope": item.scope,
        "destination": {
            "type": item.destination.get("type"),
            "url": "[configured]" if item.destination.get("type") == "webhook" else None,
        }
        if item.destination.get("type") == "webhook"
        else item.destination,
        "cooldown_seconds": item.cooldown_seconds,
        "grouping_key": item.grouping_key,
        "enabled": item.enabled,
        "created_at": item.created_at,
    }


def _occurrence_json(item: AlertOccurrence, session: Session) -> dict[str, Any]:
    deliveries = session.scalars(
        select(AlertDeliveryAttempt)
        .where(AlertDeliveryAttempt.occurrence_id == item.id)
        .order_by(AlertDeliveryAttempt.attempted_at)
    ).all()
    return {
        "id": item.id,
        "alert_group_id": item.alert_group_id,
        "rule_id": item.rule_id,
        "project_id": item.project_id,
        "fingerprint": item.fingerprint,
        "evidence": item.evidence,
        "status": item.status,
        "suppressed": item.suppressed,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
        "deliveries": [
            {
                "id": d.id,
                "destination_type": d.destination_type,
                "status": d.status,
                "response_code": d.response_code,
                "error": d.error,
                "attempted_at": d.attempted_at,
            }
            for d in deliveries
        ],
    }


@app.get("/monitoring/metrics")
async def monitoring_metrics(
    project_id: str,
    environment: str | None = None,
    application_id: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    minimum_samples: int | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    if minimum_samples is not None and minimum_samples < 1:
        raise HTTPException(status_code=422, detail="minimum_samples must be positive")
    for field_name, value in (("started_after", started_after), ("started_before", started_before)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(status_code=422, detail=f"{field_name} must include a timezone")
    if started_after and started_before and started_after > started_before:
        raise HTTPException(status_code=422, detail="started_after must be <= started_before")
    return _monitoring_service(session).metrics(
        project_id,
        MonitoringScope(
            environment=environment,
            application_id=application_id,
            agent_name=agent_name,
            agent_version=agent_version,
        ),
        started_after,
        started_before,
        minimum_samples,
    )


@app.post("/monitoring/budgets", status_code=201)
async def create_monitoring_budget(
    request: BudgetCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, request.project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{request.project_id}` was not found")
    item = _monitoring_service(session).create_budget(request)
    return {
        "id": item.id,
        "project_id": item.project_id,
        "name": item.name,
        "scope": item.scope,
        "limits": item.limits,
        "created_at": item.created_at,
    }


@app.get("/monitoring/budgets")
async def list_monitoring_budgets(
    project_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    items = session.scalars(
        select(MonitoringBudget)
        .where(MonitoringBudget.project_id == project_id)
        .order_by(MonitoringBudget.created_at)
    ).all()
    return {
        "budgets": [
            {
                "id": x.id,
                "project_id": x.project_id,
                "name": x.name,
                "scope": x.scope,
                "limits": x.limits,
                "created_at": x.created_at,
            }
            for x in items
        ]
    }


@app.get("/runs/{run_id}/budgets")
async def run_budget_status(run_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run `{run_id}` was not found")
    return {"run_id": run_id, "budgets": _monitoring_service(session).run_budgets(run)}


@app.post("/monitoring/alert-rules", status_code=201)
async def create_monitoring_alert_rule(
    request: AlertRuleCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, request.project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{request.project_id}` was not found")
    return _alert_rule_json(_monitoring_service(session).create_rule(request))


@app.get("/monitoring/alert-rules")
async def list_monitoring_alert_rules(
    project_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    return {"rules": [_alert_rule_json(x) for x in _monitoring_service(session).rules(project_id)]}


@app.post("/monitoring/alert-rules/{rule_id}/evaluate", status_code=201)
async def evaluate_monitoring_alerts(
    rule_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    rule = session.get(MonitorAlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Alert rule `{rule_id}` was not found")
    items = _monitoring_service(session).evaluate_alerts(rule.project_id, rule_id=rule_id)
    return {"occurrences": [_occurrence_json(x, session) for x in items]}


@app.get("/monitoring/alerts")
async def list_monitoring_alerts(
    project_id: str, status: str | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    query = select(AlertOccurrence).where(AlertOccurrence.project_id == project_id)
    if status:
        query = query.where(AlertOccurrence.status == status.upper())
    items = session.scalars(query.order_by(AlertOccurrence.created_at.desc())).all()
    return {"occurrences": [_occurrence_json(x, session) for x in items]}


@app.patch("/monitoring/alerts/{occurrence_id}")
async def resolve_monitoring_alert(
    occurrence_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(AlertOccurrence, occurrence_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"Alert occurrence `{occurrence_id}` was not found"
        )
    return _occurrence_json(_monitoring_service(session).resolve(item), session)


@app.post("/v1/traces", response_model=IngestResponse, status_code=202)
async def ingest_trace(
    payload: TracePayload, session: Session = Depends(get_session)
) -> IngestResponse:
    try:
        run_id = _ingest(payload, session)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return IngestResponse(accepted=[run_id])


@app.post("/v1/traces/batch", response_model=IngestResponse, status_code=207)
async def ingest_batch(request: Request, session: Session = Depends(get_session)) -> IngestResponse:
    raw = await request.body()
    if len(raw) > get_settings().max_payload_bytes:
        raise HTTPException(status_code=413, detail="Batch payload exceeds configured size limit")
    try:
        parsed = json.loads(raw)
        items = parsed.get("traces") if isinstance(parsed, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError("body must contain a non-empty `traces` array")
        if len(items) > 100:
            raise ValueError("batch may contain at most 100 traces")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    for index, item in enumerate(items):
        try:
            trace = TracePayload.model_validate(item)
            accepted.append(_ingest(trace, session))
        except PermissionError as exc:
            session.rollback()
            rejected.append({"index": str(index), "error": str(exc)[:500]})
        except (ValidationError, ValueError) as exc:
            session.rollback()
            if isinstance(exc, ValidationError):
                safe_errors = [
                    {key: detail for key, detail in error.items() if key in {"loc", "msg", "type"}}
                    for error in exc.errors(include_input=False)
                ]
                error_summary = json.dumps(safe_errors, separators=(",", ":"))[:500]
            else:
                error_summary = str(exc)[:500]
            rejected.append({"index": str(index), "error": error_summary})
    return IngestResponse(accepted=accepted, rejected=rejected)


@app.get("/runs")
async def list_runs(
    limit: int = 50,
    offset: int = 0,
    environment: str | None = None,
    application_id: str | None = None,
    agent_name: str | None = None,
    status: str | None = None,
    agent_version: str | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(
            status_code=422, detail="limit must be 1..200 and offset must be non-negative"
        )
    for field_name, value in (
        ("started_after", started_after),
        ("started_before", started_before),
    ):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(status_code=422, detail=f"{field_name} must include a timezone")
    if started_after and started_before and started_after > started_before:
        raise HTTPException(
            status_code=422, detail="started_after must be less than or equal to started_before"
        )
    query = select(Run)
    count_query = select(func.count()).select_from(Run)
    if environment:
        query = query.where(Run.environment == environment)
        count_query = count_query.where(Run.environment == environment)
    if application_id:
        query = query.where(Run.application_id == application_id)
        count_query = count_query.where(Run.application_id == application_id)
    if agent_name:
        query = query.where(Run.agent_name == agent_name)
        count_query = count_query.where(Run.agent_name == agent_name)
    if status:
        query = query.where(Run.status == status.upper())
        count_query = count_query.where(Run.status == status.upper())
    if agent_version:
        query = query.join(AgentVersion, Run.agent_version_id == AgentVersion.id).where(
            AgentVersion.version == agent_version
        )
        count_query = count_query.join(AgentVersion, Run.agent_version_id == AgentVersion.id).where(
            AgentVersion.version == agent_version
        )
    if started_after:
        query = query.where(Run.started_at >= started_after)
        count_query = count_query.where(Run.started_at >= started_after)
    if started_before:
        query = query.where(Run.started_at <= started_before)
        count_query = count_query.where(Run.started_at <= started_before)
    runs = session.scalars(query.order_by(Run.started_at.desc()).limit(limit).offset(offset)).all()
    return {
        "runs": [_run_json(run, session) for run in runs],
        "limit": limit,
        "offset": offset,
        "total": session.scalar(count_query) or 0,
    }


@app.get("/runs/{run_id}")
async def get_run(run_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run `{run_id}` was not found")
    trace = session.get(Trace, run.trace_id)
    spans = session.scalars(
        select(Span).where(Span.trace_id == run.trace_id).order_by(Span.started_at)
    ).all()
    return {
        "run": _run_json(run, session),
        "trace": _trace_json(trace),
        "spans": [_span_json(span) for span in spans],
    }


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    trace = session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Trace `{trace_id}` was not found")
    run = session.get(Run, trace.run_id)
    spans = session.scalars(
        select(Span).where(Span.trace_id == trace_id).order_by(Span.started_at)
    ).all()
    events = session.scalars(
        select(TraceEvent).where(TraceEvent.trace_id == trace_id).order_by(TraceEvent.timestamp)
    ).all()
    return {
        "trace": _trace_json(trace),
        "run": _run_json(run, session) if run else None,
        "spans": [_span_json(span) for span in spans],
        "events": [
            {
                "id": event.id,
                "span_id": event.span_id,
                "name": event.name,
                "timestamp": event.timestamp,
                "attributes": event.attributes,
            }
            for event in events
        ],
    }


@app.get("/traces/{trace_id}/spans")
async def list_spans(trace_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    if session.get(Trace, trace_id) is None:
        raise HTTPException(status_code=404, detail=f"Trace `{trace_id}` was not found")
    spans = session.scalars(
        select(Span).where(Span.trace_id == trace_id).order_by(Span.started_at)
    ).all()
    return {"trace_id": trace_id, "spans": [_span_json(span) for span in spans]}


def _dataset_json(
    dataset: EvaluationDataset, cases: list[EvaluationCase] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": dataset.id,
        "project_id": dataset.project_id,
        "name": dataset.name,
        "description": dataset.description,
        "version": dataset.version,
        "created_at": dataset.created_at,
    }
    if cases is not None:
        result["cases"] = [
            {
                "id": case.id,
                "ordinal": case.ordinal,
                "input": case.input,
                "expected_output": case.expected_output,
                "expected_output_present": case.expected_output_present,
                "expected_properties": case.expected_properties,
                "reference_context": case.reference_context,
                "metadata": case.metadata_json,
            }
            for case in cases
        ]
    return result


def _evaluator_json(evaluator: EvaluatorDefinition) -> dict[str, Any]:
    return {
        "id": evaluator.id,
        "project_id": evaluator.project_id,
        "name": evaluator.name,
        "version": evaluator.version,
        "evaluator_type": evaluator.evaluator_type,
        "config": evaluator.config,
        "created_at": evaluator.created_at,
    }


def _evaluation_json(evaluation: EvaluationRun) -> dict[str, Any]:
    return {
        "id": evaluation.id,
        "dataset_id": evaluation.dataset_id,
        "dataset_version": evaluation.dataset_version,
        "evaluator_snapshot": evaluation.evaluator_snapshot,
        "status": evaluation.status,
        "total_results": evaluation.total_results,
        "passed": evaluation.passed,
        "failed": evaluation.failed,
        "unavailable": evaluation.unavailable,
        "assessed": evaluation.assessed,
        "created_at": evaluation.created_at,
        "completed_at": evaluation.completed_at,
    }


def _evaluation_service(session: Session) -> EvaluationService:
    return EvaluationService(session, _redactor())


@app.post("/evaluations/datasets", status_code=201)
async def create_evaluation_dataset(
    request: DatasetCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, request.project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{request.project_id}` was not found")
    try:
        dataset = _evaluation_service(session).create_dataset(request)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Dataset version conflicts with an existing version"
        ) from exc
    return _dataset_json(dataset)


@app.get("/evaluations/datasets")
async def list_evaluation_datasets(
    project_id: str | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    query = select(EvaluationDataset)
    if project_id:
        query = query.where(EvaluationDataset.project_id == project_id)
    datasets = session.scalars(
        query.order_by(EvaluationDataset.name, EvaluationDataset.version.desc())
    ).all()
    return {"datasets": [_dataset_json(dataset) for dataset in datasets]}


@app.get("/evaluations/datasets/{dataset_id}")
async def get_evaluation_dataset(
    dataset_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    dataset = session.get(EvaluationDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"Dataset `{dataset_id}` was not found")
    cases = session.scalars(
        select(EvaluationCase)
        .where(EvaluationCase.dataset_id == dataset.id)
        .order_by(EvaluationCase.ordinal)
    ).all()
    return _dataset_json(dataset, list(cases))


@app.post("/evaluations/evaluators", status_code=201)
async def create_evaluator(
    request: EvaluatorCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, request.project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{request.project_id}` was not found")
    try:
        evaluator = _evaluation_service(session).create_evaluator(request)
    except EvaluationConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Evaluator version conflicts with an existing version"
        ) from exc
    return _evaluator_json(evaluator)


@app.get("/evaluations/evaluators")
async def list_evaluators(
    project_id: str | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    query = select(EvaluatorDefinition)
    if project_id:
        query = query.where(EvaluatorDefinition.project_id == project_id)
    evaluators = session.scalars(
        query.order_by(EvaluatorDefinition.name, EvaluatorDefinition.version.desc())
    ).all()
    return {"evaluators": [_evaluator_json(evaluator) for evaluator in evaluators]}


@app.post("/evaluations", status_code=201)
async def create_evaluation(
    request: EvaluationCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        evaluation = _evaluation_service(session).run(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _evaluation_json(evaluation)


@app.get("/evaluations")
async def list_evaluations(
    dataset_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(
            status_code=422, detail="limit must be 1..200 and offset must be non-negative"
        )
    query = select(EvaluationRun)
    if dataset_id:
        query = query.where(EvaluationRun.dataset_id == dataset_id)
    runs = session.scalars(
        query.order_by(EvaluationRun.created_at.desc()).limit(limit).offset(offset)
    ).all()
    total_query = select(func.count()).select_from(EvaluationRun)
    if dataset_id:
        total_query = total_query.where(EvaluationRun.dataset_id == dataset_id)
    return {
        "evaluations": [_evaluation_json(item) for item in runs],
        "limit": limit,
        "offset": offset,
        "total": session.scalar(total_query) or 0,
    }


@app.get("/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    evaluation = session.get(EvaluationRun, evaluation_id)
    if evaluation is None:
        raise HTTPException(status_code=404, detail=f"Evaluation `{evaluation_id}` was not found")
    results = session.scalars(
        select(EvaluationResult)
        .where(EvaluationResult.evaluation_run_id == evaluation.id)
        .order_by(EvaluationResult.case_id, EvaluationResult.evaluator_id)
    ).all()
    case_ids = {result.case_id for result in results}
    cases = (
        {
            case.id: case
            for case in session.scalars(
                select(EvaluationCase).where(EvaluationCase.id.in_(case_ids))
            )
        }
        if case_ids
        else {}
    )
    return {
        **_evaluation_json(evaluation),
        "results": [
            {
                "id": result.id,
                "case_id": result.case_id,
                "dataset_version": result.dataset_version,
                "case": {
                    "input": cases[result.case_id].input,
                    "expected_output": cases[result.case_id].expected_output,
                    "expected_output_present": cases[result.case_id].expected_output_present,
                    "expected_properties": cases[result.case_id].expected_properties,
                    "reference_context": cases[result.case_id].reference_context,
                    "metadata": cases[result.case_id].metadata_json,
                }
                if result.case_id in cases
                else None,
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


def _visible_candidate_runs(
    candidates: list[dict[str, Any]], session: Session
) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        case_runs = item.get("case_runs")
        if isinstance(case_runs, dict):
            item["case_runs"] = {
                case_id: run_id
                for case_id, run_id in case_runs.items()
                if isinstance(run_id, str) and session.get(Run, run_id) is not None
            }
        visible.append(item)
    return visible


def _experiment_json(experiment: Experiment, session: Session) -> dict[str, Any]:
    return {
        "id": experiment.id,
        "project_id": experiment.project_id,
        "name": experiment.name,
        "version": experiment.version,
        "dataset_id": experiment.dataset_id,
        "dataset_version": experiment.dataset_version,
        "baseline_candidate_id": experiment.baseline_candidate_id,
        "evaluator_snapshot": experiment.evaluator_snapshot,
        "candidates": _visible_candidate_runs(experiment.candidates, session),
        "criteria": experiment.criteria,
        "created_at": experiment.created_at,
    }


def _experiment_run_json(item: ExperimentRun, session: Session) -> dict[str, Any]:
    return {
        "id": item.id,
        "experiment_id": item.experiment_id,
        "experiment_version": item.experiment_version,
        "dataset_id": item.dataset_id,
        "dataset_version": item.dataset_version,
        "evaluator_snapshot": item.evaluator_snapshot,
        "candidate_snapshot": _visible_candidate_runs(item.candidate_snapshot, session),
        "criteria_snapshot": item.criteria_snapshot,
        "status": item.status,
        "comparison": item.comparison,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
    }


@app.post("/experiments", status_code=201)
async def create_experiment(
    request: ExperimentCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, request.project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{request.project_id}` was not found")
    try:
        return _experiment_json(ExperimentService(session).create(request), session)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Experiment version conflict") from exc


@app.get("/experiments")
async def list_experiments(
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(status_code=422, detail="limit must be 1..200 and offset non-negative")
    query = select(Experiment)
    count = select(func.count()).select_from(Experiment)
    if project_id:
        query = query.where(Experiment.project_id == project_id)
        count = count.where(Experiment.project_id == project_id)
    items = session.scalars(
        query.order_by(Experiment.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "experiments": [_experiment_json(item, session) for item in items],
        "limit": limit,
        "offset": offset,
        "total": session.scalar(count) or 0,
    }


@app.get("/experiments/{experiment_id}")
async def get_experiment(
    experiment_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(Experiment, experiment_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Experiment `{experiment_id}` was not found")
    return _experiment_json(item, session)


@app.post("/experiments/{experiment_id}/compare", status_code=201)
async def compare_experiment(
    experiment_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(Experiment, experiment_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Experiment `{experiment_id}` was not found")
    try:
        return _experiment_run_json(ExperimentService(session).run(item), session)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/experiments/{experiment_id}/runs")
async def list_experiment_runs(
    experiment_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Experiment, experiment_id) is None:
        raise HTTPException(status_code=404, detail=f"Experiment `{experiment_id}` was not found")
    runs = session.scalars(
        select(ExperimentRun)
        .where(ExperimentRun.experiment_id == experiment_id)
        .order_by(ExperimentRun.created_at.desc())
    ).all()
    return {"runs": [_experiment_run_json(item, session) for item in runs], "total": len(runs)}


@app.get("/experiment-runs/{experiment_run_id}")
async def get_experiment_run(
    experiment_run_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(ExperimentRun, experiment_run_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"Experiment run `{experiment_run_id}` was not found"
        )
    results = session.scalars(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_run_id == item.id)
        .order_by(
            ExperimentResult.candidate_id, ExperimentResult.case_id, ExperimentResult.evaluator_id
        )
    ).all()
    return {
        **_experiment_run_json(item, session),
        "results": [
            {
                "id": result.id,
                "candidate_id": result.candidate_id,
                "case_id": result.case_id,
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


def _agent_analysis_json(record: AgentAnalysis) -> dict[str, Any]:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "project_id": record.project_id,
        "config": record.config_snapshot,
        "analysis": record.analysis,
        "created_at": record.created_at,
    }


def _policy_json(item: GovernancePolicy) -> dict[str, Any]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "name": item.name,
        "version": item.version,
        "description": item.description,
        "conditions": item.conditions,
        "effect": item.effect,
        "severity": item.severity,
        "created_at": item.created_at,
    }


def _violation_json(item: PolicyViolation) -> dict[str, Any]:
    return {
        "id": item.id,
        "policy_evaluation_id": item.policy_evaluation_id,
        "run_id": item.run_id,
        "project_id": item.project_id,
        "policy_id": item.policy_id,
        "policy_version": item.policy_version,
        "policy_snapshot": item.policy_snapshot,
        "effect": item.effect,
        "severity": item.severity,
        "evidence": item.evidence,
        "status": item.status,
        "review_required": item.review_required,
        "waiver": {
            "actor": item.waiver_actor,
            "reason": item.waiver_reason,
            "waived_at": item.waived_at,
            "expires_at": item.waiver_expires_at,
            "expired": bool(
                item.waiver_expires_at
                and (
                    item.waiver_expires_at.replace(tzinfo=UTC)
                    if item.waiver_expires_at.tzinfo is None
                    else item.waiver_expires_at
                )
                <= datetime.now(UTC)
            ),
        }
        if item.status == "WAIVED"
        else None,
        "created_at": item.created_at,
    }


def _policy_evaluation_json(
    item: PolicyEvaluation, violation: PolicyViolation | None = None
) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "project_id": item.project_id,
        "policy_id": item.policy_id,
        "policy_version": item.policy_version,
        "policy_snapshot": item.policy_snapshot,
        "result": item.result,
        "decision": item.decision,
        "evidence": item.evidence,
        "created_at": item.created_at,
        "violation": _violation_json(violation) if violation else None,
    }


@app.post("/policies", status_code=201)
async def create_policy(
    request: PolicyCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Project, request.project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{request.project_id}` was not found")
    try:
        item = GovernanceService(session).create_policy(request)
        session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Policy version conflicts with an existing version"
        ) from exc
    return _policy_json(item)


@app.get("/policies")
async def list_policies(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    items = session.scalars(
        select(GovernancePolicy)
        .where(GovernancePolicy.project_id == project_id)
        .order_by(GovernancePolicy.name, GovernancePolicy.version.desc())
    ).all()
    return {"policies": [_policy_json(item) for item in items]}


@app.get("/policies/{policy_id}")
async def get_policy(policy_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    item = session.get(GovernancePolicy, policy_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Policy `{policy_id}` was not found")
    return _policy_json(item)


@app.post("/runs/{run_id}/policy-check", status_code=201)
async def evaluate_run_policies(
    run_id: str, request: PolicyCheckRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        items = GovernanceService(session).evaluate(run_id, request.policy_ids)
        session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "run_id": run_id,
        "evaluations": [
            _policy_evaluation_json(evaluation, violation) for evaluation, violation in items
        ],
    }


@app.get("/runs/{run_id}/policy-checks")
async def list_run_policy_checks(
    run_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Run, run_id) is None:
        raise HTTPException(status_code=404, detail=f"Run `{run_id}` was not found")
    items = session.scalars(
        select(PolicyEvaluation)
        .where(PolicyEvaluation.run_id == run_id)
        .order_by(PolicyEvaluation.created_at)
    ).all()
    result = []
    for item in items:
        violation = session.scalar(
            select(PolicyViolation).where(PolicyViolation.policy_evaluation_id == item.id)
        )
        result.append(_policy_evaluation_json(item, violation))
    return {"run_id": run_id, "evaluations": result}


@app.get("/policy-evaluations/{evaluation_id}")
async def get_policy_evaluation(
    evaluation_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(PolicyEvaluation, evaluation_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"Policy evaluation `{evaluation_id}` was not found"
        )
    violation = session.scalar(
        select(PolicyViolation).where(PolicyViolation.policy_evaluation_id == item.id)
    )
    return _policy_evaluation_json(item, violation)


@app.get("/violations")
async def list_policy_violations(
    project_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(status_code=422, detail="limit must be 1..200 and offset non-negative")
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    query = select(PolicyViolation).where(PolicyViolation.project_id == project_id)
    if status:
        normalized = status.upper()
        if normalized not in {"OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"}:
            raise HTTPException(status_code=422, detail="Unsupported violation status")
        query = query.where(PolicyViolation.status == normalized)
    items = session.scalars(
        query.order_by(PolicyViolation.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "violations": [_violation_json(item) for item in items],
        "limit": limit,
        "offset": offset,
    }


@app.get("/violations/{violation_id}")
async def get_policy_violation(
    violation_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(PolicyViolation, violation_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Violation `{violation_id}` was not found")
    return _violation_json(item)


@app.patch("/violations/{violation_id}")
async def transition_policy_violation(
    violation_id: str, request: ViolationTransitionRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    item = session.get(PolicyViolation, violation_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Violation `{violation_id}` was not found")
    try:
        GovernanceService(session).transition(item, request)
        session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _violation_json(item)


@app.get("/violations/{violation_id}/history")
async def get_policy_violation_history(
    violation_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(PolicyViolation, violation_id) is None:
        raise HTTPException(status_code=404, detail=f"Violation `{violation_id}` was not found")
    items = session.scalars(
        select(PolicyTransition)
        .where(PolicyTransition.violation_id == violation_id)
        .order_by(PolicyTransition.created_at, PolicyTransition.id)
    ).all()
    return {
        "violation_id": violation_id,
        "history": [
            {
                "id": item.id,
                "from_status": item.from_status,
                "to_status": item.to_status,
                "actor": item.actor,
                "reason": item.reason,
                "created_at": item.created_at,
                "expires_at": item.expires_at,
            }
            for item in items
        ],
    }


@app.post("/runs/{run_id}/agent-analytics", status_code=201)
async def analyze_agent_run(
    run_id: str,
    request: AgentAnalyticsRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        return _agent_analysis_json(AgentAnalyticsService(session).analyze(run_id, request.config))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/runs/{run_id}/agent-analytics")
async def get_agent_run_analytics(
    run_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if session.get(Run, run_id) is None:
        raise HTTPException(status_code=404, detail=f"Run `{run_id}` was not found")
    record = AgentAnalyticsService(session).latest(run_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No agent analytics have been computed for run `{run_id}`",
        )
    return _agent_analysis_json(record)


@app.get("/agent-analytics")
async def get_agent_analytics_aggregate(
    project_id: str,
    application_id: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        filters = AgentAnalyticsFilters(
            project_id=project_id,
            application_id=application_id,
            agent_name=agent_name,
            agent_version=agent_version,
            started_after=started_after,
            started_before=started_before,
        )
        if session.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
        return AgentAnalyticsService(session).aggregate(filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _trace_json(trace: Trace | None) -> dict[str, Any] | None:
    if trace is None:
        return None
    return {
        "trace_id": trace.trace_id,
        "run_id": trace.run_id,
        "root_span_id": trace.root_span_id,
        "started_at": trace.started_at,
        "ended_at": trace.ended_at,
        "duration_ms": trace.duration_ms,
        "status": trace.status,
        "attributes": trace.attributes,
    }


@app.get("/audit/events")
async def list_audit_events(
    project_id: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    principal = session.info.get("principal")
    query = select(AuditEvent)
    if principal and principal.organization_id:
        query = query.where(AuditEvent.organization_id == principal.organization_id)
    if project_id:
        query = query.where(AuditEvent.project_id == project_id)
    rows = session.scalars(query.order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    return {
        "events": [
            {
                "id": row.id,
                "project_id": row.project_id,
                "actor": row.actor,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "details": row.details,
                "previous_hash": row.previous_hash,
                "entry_hash": row.entry_hash,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@app.get("/audit/verify")
async def audit_verify(session: Session = Depends(get_session)) -> dict[str, Any]:
    principal = session.info.get("principal")
    if principal is None or not principal.organization_id:
        raise HTTPException(status_code=403, detail="An organization-scoped principal is required")
    if not principal.development:
        previous_scope = session.info.get("skip_scope", False)
        session.info["skip_scope"] = True
        try:
            organization_projects = set(
                session.scalars(
                    select(Project.id).where(Project.organization_id == principal.organization_id)
                )
            )
            organization_events = session.scalars(
                select(AuditEvent).where(AuditEvent.organization_id == principal.organization_id)
            ).all()
            event_environments = {
                environment
                for event in organization_events
                if isinstance((environment := event.details.get("environment")), str)
            }
        finally:
            session.info["skip_scope"] = previous_scope
        if not organization_projects.issubset(
            principal.project_ids
        ) or not event_environments.issubset(principal.environments):
            raise HTTPException(
                status_code=403,
                detail="Audit-chain verification requires scope to every project and environment in the organization",
            )
    return verify_audit(session, principal.organization_id)


@app.delete("/projects/{project_id}/runs")
async def delete_project_runs(
    project_id: str,
    environment: str,
    before: datetime,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    principal = session.info.get("principal")
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        result = delete_runs_before(
            session, principal, project_id=project_id, environment=environment, before=before
        )
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/projects/{project_id}/retention/enforce")
async def enforce_project_retention(
    project_id: str,
    environment: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project `{project_id}` was not found")
    try:
        policies = retention_days_by_environment(get_settings().retention_days_by_environment)
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="Configured retention policy is invalid"
        ) from exc
    days = policies.get(environment)
    if days is None:
        raise HTTPException(
            status_code=422, detail=f"No retention period is configured for `{environment}`"
        )
    principal = session.info.get("principal")
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        result = delete_runs_before(
            session,
            principal,
            project_id=project_id,
            environment=environment,
            before=retention_cutoff(days),
        )
        session.commit()
        return {**result, "retention_days": days}
    except (ValueError, PermissionError) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
