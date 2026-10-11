"""Stored-data production metrics, run budgets, and deterministic alerting."""

import hashlib
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tracecore.models import (
    AgentAnalysis,
    AgentVersion,
    AlertDeliveryAttempt,
    AlertOccurrence,
    MonitorAlertRule,
    MonitoringBudget,
    PolicyEvaluation,
    PolicyViolation,
    Run,
    Span,
    TraceEvent,
    new_id,
)
from tracecore.monitoring_schemas import AlertRuleCreate, BudgetCreate, MonitoringScope

logger = logging.getLogger("tracecore.monitoring")


class MonitoringService:
    def __init__(self, session: Session, percentile_min_samples: int = 20) -> None:
        self.session = session
        self.percentile_min_samples = percentile_min_samples

    def metrics(
        self,
        project_id: str,
        scope: MonitoringScope | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        minimum_samples: int | None = None,
    ) -> dict[str, Any]:
        scope = scope or MonitoringScope()
        query = select(Run).where(Run.project_id == project_id)
        query = self._scope_query(query, scope)
        if started_after:
            query = query.where(Run.started_at >= started_after)
        if started_before:
            query = query.where(Run.started_at <= started_before)
        runs = list(self.session.scalars(query.order_by(Run.started_at)).all())
        minimum = minimum_samples or self.percentile_min_samples
        run_ids = [run.id for run in runs]
        traces = [run.trace_id for run in runs]
        spans = (
            list(self.session.scalars(select(Span).where(Span.trace_id.in_(traces))).all())
            if traces
            else []
        )
        by_trace: dict[str, list[Span]] = {}
        for span in spans:
            by_trace.setdefault(span.trace_id, []).append(span)
        statuses = [run.status.upper() for run in runs]
        duration = [run.duration_ms for run in runs if run.duration_ms is not None]
        retries: list[float] = []
        retry_observed = 0
        token_values: list[float] = []
        verified_costs: list[float] = []
        model_calls = tool_calls = tool_failures = model_failures = 0
        model_observed = tool_observed = 0
        timeout_count = 0
        timeout_observed = 0
        critical_values: list[bool] = []
        for run in runs:
            run_spans = by_trace.get(run.trace_id, [])
            llm = [s for s in run_spans if s.span_type.upper() == "LLM"]
            tools = [s for s in run_spans if s.span_type.upper() == "TOOL"]
            model_calls += len(llm)
            tool_calls += len(tools)
            if llm:
                model_observed += 1
            if tools:
                tool_observed += 1
            model_failures += sum(s.status.upper() in {"FAILED", "ERROR", "TIMEOUT"} for s in llm)
            tool_failures += sum(s.status.upper() in {"FAILED", "ERROR", "TIMEOUT"} for s in tools)
            run_token_total = 0.0
            found_tokens = False
            for span in llm:
                attrs = span.attributes or {}
                for key in (
                    "total_tokens",
                    "gen_ai.usage.total_tokens",
                    "token_usage.total_tokens",
                ):
                    value = attrs.get(key)
                    if isinstance(value, (int, float)) and value >= 0:
                        run_token_total += float(value)
                        found_tokens = True
                        break
                if not found_tokens:
                    inp = attrs.get("input_tokens", attrs.get("gen_ai.usage.input_tokens"))
                    out = attrs.get("output_tokens", attrs.get("gen_ai.usage.output_tokens"))
                    if (
                        isinstance(inp, (int, float))
                        and isinstance(out, (int, float))
                        and inp >= 0
                        and out >= 0
                    ):
                        run_token_total += float(inp + out)
                        found_tokens = True
            usage = run.token_usage if isinstance(run.token_usage, dict) else {}
            total = usage.get("total_tokens")
            if isinstance(total, (int, float)) and total >= 0:
                run_token_total = float(total)
                found_tokens = True
            if found_tokens:
                token_values.append(run_token_total)
            run_metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
            timeout_flags = [run_metadata["timeout"]] if "timeout" in run_metadata else []
            timeout_flags.extend(
                (span.attributes or {})["timeout"]
                for span in run_spans
                if "timeout" in (span.attributes or {})
            )
            if timeout_flags:
                timeout_observed += 1
                if any(value is True for value in timeout_flags):
                    timeout_count += 1
            critical = run_metadata.get("critical")
            if isinstance(critical, bool):
                critical_values.append(critical and run.status.upper() in {"FAILED", "ERROR"})
            retry_count, observed = self._retries(run_spans, run.trace_id)
            if observed:
                retry_observed += 1
                retries.append(float(retry_count))
            meta = run.metadata_json if isinstance(run.metadata_json, dict) else {}
            verified = meta.get("verified_cost") if meta.get("cost_verified") is True else None
            if isinstance(verified, (int, float)) and verified >= 0:
                verified_costs.append(float(verified))
        # The selected run IDs carry the project and dimension scope into this count.
        violation_count = (
            len(
                self.session.scalars(
                    select(PolicyViolation.id).where(PolicyViolation.run_id.in_(run_ids))
                ).all()
            )
            if run_ids
            else 0
        )
        evaluated_run_ids = (
            set(
                self.session.scalars(
                    select(PolicyEvaluation.run_id).where(PolicyEvaluation.run_id.in_(run_ids))
                ).all()
            )
            if run_ids
            else set()
        )
        result: dict[str, Any] = {
            "project_id": project_id,
            "scope": scope.model_dump(exclude_none=True),
            "run_count": len(runs),
            "window": {"started_after": started_after, "started_before": started_before},
            "metrics": {
                "success_count": self._metric(
                    sum(s == "SUCCEEDED" for s in statuses), len(runs), population=len(runs)
                ),
                "failure_count": self._metric(
                    sum(s == "FAILED" for s in statuses), len(runs), population=len(runs)
                ),
                "timeout_count": self._metric(
                    timeout_count, timeout_observed, population=len(runs)
                ),
                "critical_failure_rate": self._metric(
                    sum(critical_values) / len(critical_values) if critical_values else 0.0,
                    len(critical_values),
                    "runs_with_explicit_critical_classification",
                    len(runs),
                ),
                "retry_count": self._metric(
                    sum(retries), retry_observed, "runs_with_explicit_retry_telemetry", len(runs)
                ),
                "tool_failure_count": self._metric(
                    tool_failures,
                    sum(
                        bool(
                            [
                                s
                                for s in by_trace.get(run.trace_id, [])
                                if s.span_type.upper() == "TOOL"
                            ]
                        )
                        for run in runs
                    ),
                    "runs_with_tool_spans",
                    len(runs),
                ),
                "model_failure_count": self._metric(
                    model_failures, model_observed, "runs_with_llm_spans", len(runs)
                ),
                "policy_violation_count": self._metric(
                    violation_count, len(evaluated_run_ids), population=len(runs)
                ),
                "model_calls": self._metric(
                    model_calls, model_observed, "runs_with_llm_spans", len(runs)
                ),
                "tool_calls": self._metric(
                    tool_calls, tool_observed, "runs_with_tool_spans", len(runs)
                ),
                "tokens": self._metric(
                    sum(token_values), len(token_values), "runs_with_token_telemetry", len(runs)
                ),
                "verified_cost": self._metric(
                    sum(verified_costs), len(verified_costs), "runs_with_verified_cost", len(runs)
                ),
                "latency_ms": {
                    "sample_count": len(duration),
                    "minimum_samples": minimum,
                    "status": "AVAILABLE" if len(duration) >= minimum else "UNAVAILABLE",
                    "method": "nearest_rank",
                    "p50": self._percentile(duration, 0.50) if len(duration) >= minimum else None,
                    "p95": self._percentile(duration, 0.95) if len(duration) >= minimum else None,
                    "p99": self._percentile(duration, 0.99) if len(duration) >= minimum else None,
                },
            },
        }
        result["budgets"] = [
            self.check_budget(budget, runs, by_trace) for budget in self._budgets(project_id, scope)
        ]
        return result

    @staticmethod
    def _metric(
        value: float,
        sample_count: int,
        sample_name: str = "runs",
        population: int | None = None,
    ) -> dict[str, Any]:
        status = (
            "UNAVAILABLE"
            if not sample_count
            else "PARTIAL"
            if population is not None and sample_count < population
            else "AVAILABLE"
        )
        return {
            "value": value if sample_count else None,
            "status": status,
            "sample_count": sample_count,
            "sample_name": sample_name,
        }

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        ordered = sorted(float(value) for value in values)
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    @staticmethod
    def _scope_query(query: Any, scope: MonitoringScope) -> Any:
        if scope.environment:
            query = query.where(Run.environment == scope.environment)
        if scope.application_id:
            query = query.where(Run.application_id == scope.application_id)
        if scope.agent_name:
            query = query.where(Run.agent_name == scope.agent_name)
        if scope.agent_version:
            query = query.join(AgentVersion, Run.agent_version_id == AgentVersion.id).where(
                AgentVersion.version == scope.agent_version
            )
        return query

    def _budgets(self, project_id: str, scope: MonitoringScope) -> list[MonitoringBudget]:
        values = self.session.scalars(
            select(MonitoringBudget).where(MonitoringBudget.project_id == project_id)
        ).all()
        return [
            b
            for b in values
            if all(
                scope.model_dump(exclude_none=True).get(k) in (None, v) for k, v in b.scope.items()
            )
        ]

    def check_budget(
        self, budget: MonitoringBudget, runs: list[Run], by_trace: dict[str, list[Span]]
    ) -> dict[str, Any]:
        selected = [
            run
            for run in runs
            if all(
                getattr(run, k, None) == v
                for k, v in budget.scope.items()
                if k in {"environment", "application_id", "agent_name"}
            )
            and (
                "agent_version" not in budget.scope
                or (
                    run.agent_version_id is not None
                    and (version := self.session.get(AgentVersion, run.agent_version_id))
                    is not None
                    and version.version == budget.scope["agent_version"]
                )
            )
        ]
        usage: dict[str, Any] = {}
        for key in budget.limits:
            observed: list[float] = []
            for run in selected:
                spans = by_trace.get(run.trace_id, [])
                if key == "model_calls":
                    observed.append(float(sum(s.span_type.upper() == "LLM" for s in spans)))
                elif key == "tool_calls":
                    observed.append(float(sum(s.span_type.upper() == "TOOL" for s in spans)))
                elif key == "duration_ms" and run.duration_ms is not None:
                    observed.append(float(run.duration_ms))
                elif key == "verified_cost":
                    metadata = run.metadata_json or {}
                    if metadata.get("cost_verified") is True and isinstance(
                        metadata.get("verified_cost"), (int, float)
                    ):
                        observed.append(float(metadata["verified_cost"]))
                elif key == "tokens":
                    total = (run.token_usage or {}).get("total_tokens")
                    if not isinstance(total, (int, float)):
                        span_totals = [
                            (span.attributes or {}).get("total_tokens")
                            for span in spans
                            if span.span_type.upper() == "LLM"
                        ]
                        valid_totals = [
                            item
                            for item in span_totals
                            if isinstance(item, (int, float)) and item >= 0
                        ]
                        total = sum(valid_totals) if valid_totals else None
                    if isinstance(total, (int, float)):
                        observed.append(float(total))
                elif key == "retries":
                    count, available = self._retries(spans, run.trace_id)
                    if available:
                        observed.append(float(count))
            limit = float(budget.limits[key])
            value = observed[0] if len(observed) == 1 else None
            usage[key] = {
                "value": value,
                "limit": limit,
                "status": "UNAVAILABLE" if not observed else "AVAILABLE",
                "exceeded": any(value > limit for value in observed) if observed else None,
                "sample_count": len(observed),
                "exceeded_runs": sum(value > limit for value in observed),
            }
        return {"id": budget.id, "name": budget.name, "scope": budget.scope, "usage": usage}

    def run_budgets(self, run: Run) -> list[dict[str, Any]]:
        spans = list(self.session.scalars(select(Span).where(Span.trace_id == run.trace_id)).all())
        applicable = self._budgets(
            run.project_id,
            MonitoringScope(
                environment=run.environment,
                application_id=run.application_id,
                agent_name=run.agent_name,
            ),
        )
        return [self.check_budget(budget, [run], {run.trace_id: spans}) for budget in applicable]

    def _retries(self, spans: list[Span], trace_id: str) -> tuple[int, bool]:
        count, observed = 0, False
        events = self.session.scalars(
            select(TraceEvent).where(TraceEvent.trace_id == trace_id)
        ).all()
        by_span: dict[str, list[TraceEvent]] = {}
        for event in events:
            if event.span_id and (
                event.name.lower() == "retry" or event.name.lower().endswith(".retry")
            ):
                by_span.setdefault(event.span_id, []).append(event)
        covered: set[str] = set()
        for span in spans:
            attrs = span.attributes or {}
            explicit: int | None = None
            for key in ("retry_count", "retry_attempt", "attempt"):
                value = attrs.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    explicit = (
                        max(0, value - 1) if key in {"retry_attempt", "attempt"} else max(0, value)
                    )
                    break
            if attrs.get("retry") is True:
                explicit = max(explicit or 0, 1)
            span_events = by_span.get(span.id, [])
            covered.update(event.id for event in span_events)
            if explicit is not None or span_events:
                count += max(explicit or 0, len(span_events))
                observed = True
        for event in events:
            if event.id not in covered and (
                event.name.lower() == "retry" or event.name.lower().endswith(".retry")
            ):
                count += 1
                observed = True
        return count, observed

    def create_budget(self, request: BudgetCreate) -> MonitoringBudget:
        item = MonitoringBudget(
            project_id=request.project_id,
            name=request.name,
            scope=request.scope.model_dump(exclude_none=True),
            limits=request.limits,
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def create_rule(self, request: AlertRuleCreate) -> MonitorAlertRule:
        item = MonitorAlertRule(
            project_id=request.project_id,
            name=request.name,
            metric=request.metric,
            operator=request.operator,
            threshold=request.threshold,
            window_minutes=request.window_minutes,
            minimum_samples=request.minimum_samples,
            severity=request.severity,
            scope=request.scope.model_dump(exclude_none=True),
            destination=request.destination,
            cooldown_seconds=request.cooldown_seconds,
            grouping_key=request.grouping_key,
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def rules(self, project_id: str) -> list[MonitorAlertRule]:
        return list(
            self.session.scalars(
                select(MonitorAlertRule)
                .where(MonitorAlertRule.project_id == project_id)
                .order_by(MonitorAlertRule.created_at)
            ).all()
        )

    def evaluate_alerts(
        self, project_id: str, now: datetime | None = None, rule_id: str | None = None
    ) -> list[AlertOccurrence]:
        now = now or datetime.now(UTC)
        occurrences: list[AlertOccurrence] = []
        for rule in self.rules(project_id):
            if rule_id is not None and rule.id != rule_id:
                continue
            if not rule.enabled:
                continue
            scope = MonitoringScope.model_validate(rule.scope or {})
            after = now - timedelta(minutes=rule.window_minutes)
            metrics = self.metrics(project_id, scope, after, now, rule.minimum_samples)["metrics"]
            measured = self._rule_value(rule, metrics, after, now, scope)
            if measured is None:
                continue
            value, samples, evidence = measured
            if samples < rule.minimum_samples or not self._compare(
                value, rule.operator, rule.threshold
            ):
                continue
            group_value = self._group_value(rule, scope)
            fingerprint = hashlib.sha256(f"{rule.id}:{group_value}".encode()).hexdigest()
            previous = self.session.scalar(
                select(AlertOccurrence)
                .where(AlertOccurrence.fingerprint == fingerprint, AlertOccurrence.status == "OPEN")
                .order_by(AlertOccurrence.created_at.desc())
            )
            cooldown = (
                previous is not None
                and (now - self._aware(previous.created_at)).total_seconds() < rule.cooldown_seconds
            )
            record = AlertOccurrence(
                alert_group_id=previous.alert_group_id if previous else new_id(),
                rule_id=rule.id,
                project_id=project_id,
                fingerprint=fingerprint,
                evidence={
                    "metric": rule.metric,
                    "value": value,
                    "threshold": rule.threshold,
                    "operator": rule.operator,
                    "window_minutes": rule.window_minutes,
                    "sample_count": samples,
                    "scope": scope.model_dump(exclude_none=True),
                    **evidence,
                },
                status="OPEN",
                suppressed=cooldown,
                created_at=now,
            )
            self.session.add(record)
            self.session.flush()
            if not cooldown:
                self._deliver(rule, record)
            occurrences.append(record)
        self.session.commit()
        return occurrences

    def _rule_value(
        self,
        rule: MonitorAlertRule,
        metrics: dict[str, Any],
        after: datetime,
        now: datetime,
        scope: MonitoringScope,
    ) -> tuple[float, int, dict[str, Any]] | None:
        metric = rule.metric
        if metric.startswith("latency_"):
            field = metric.removeprefix("latency_").removesuffix("_ms")
            value = metrics["latency_ms"].get(field)
            return (
                (float(value), metrics["latency_ms"]["sample_count"], {})
                if value is not None
                else None
            )
        if metric == "failure_rate":
            failed = metrics["failure_count"]["value"]
            total = metrics["failure_count"]["sample_count"]
            return (float(failed) / total, total, {"failed_runs": failed}) if total else None
        if metric == "critical_failure_rate":
            item = metrics["critical_failure_rate"]
            return (
                (float(item["value"]), int(item["sample_count"]), {})
                if item["status"] == "AVAILABLE" and item["value"] is not None
                else None
            )
        if metric == "policy_violations":
            policy_query = (
                select(PolicyViolation.id)
                .join(Run, Run.id == PolicyViolation.run_id)
                .where(
                    PolicyViolation.project_id == rule.project_id,
                    PolicyViolation.created_at >= after,
                    PolicyViolation.created_at <= now,
                )
            )
            if scope.environment:
                policy_query = policy_query.where(Run.environment == scope.environment)
            if scope.application_id:
                policy_query = policy_query.where(Run.application_id == scope.application_id)
            if scope.agent_name:
                policy_query = policy_query.where(Run.agent_name == scope.agent_name)
            if scope.agent_version:
                policy_query = policy_query.join(
                    AgentVersion, Run.agent_version_id == AgentVersion.id
                ).where(AgentVersion.version == scope.agent_version)
            ids = self.session.scalars(policy_query).all()
            return float(len(ids)), len(ids), {"violation_count": len(ids)}
        if metric == "verified_cost":
            return (
                (
                    float(metrics["verified_cost"]["value"]),
                    metrics["verified_cost"]["sample_count"],
                    {},
                )
                if metrics["verified_cost"]["status"] == "AVAILABLE"
                and metrics["verified_cost"]["value"] is not None
                else None
            )
        if metric == "runaway_loops":
            loop_query = (
                select(AgentAnalysis)
                .join(Run, Run.id == AgentAnalysis.run_id)
                .where(
                    AgentAnalysis.project_id == rule.project_id,
                    AgentAnalysis.created_at >= after,
                    AgentAnalysis.created_at <= now,
                )
                .order_by(AgentAnalysis.created_at.desc())
            )
            if scope.environment:
                loop_query = loop_query.where(Run.environment == scope.environment)
            if scope.application_id:
                loop_query = loop_query.where(Run.application_id == scope.application_id)
            if scope.agent_name:
                loop_query = loop_query.where(Run.agent_name == scope.agent_name)
            if scope.agent_version:
                loop_query = loop_query.join(
                    AgentVersion, Run.agent_version_id == AgentVersion.id
                ).where(AgentVersion.version == scope.agent_version)
            count = 0
            observed_runs: set[str] = set()
            for analysis in self.session.scalars(loop_query).all():
                if analysis.run_id in observed_runs:
                    continue
                observed_runs.add(analysis.run_id)
                events = analysis.analysis.get("loop_events", [])
                count += len(events)
            return float(count), count, {"loop_event_count": count}
        return None

    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        return {
            "gt": value > threshold,
            "gte": value >= threshold,
            "lt": value < threshold,
            "lte": value <= threshold,
        }[operator]

    @staticmethod
    def _group_value(rule: MonitorAlertRule, scope: MonitoringScope) -> str:
        return (
            getattr(
                scope,
                rule.grouping_key if rule.grouping_key != "version" else "agent_version",
                None,
            )
            or rule.grouping_key
        )

    def _deliver(self, rule: MonitorAlertRule, occurrence: AlertOccurrence) -> None:
        kind = rule.destination.get("type")
        body = {
            "occurrence_id": occurrence.id,
            "rule_id": rule.id,
            "fingerprint": occurrence.fingerprint,
            "severity": rule.severity,
            "evidence": occurrence.evidence,
        }
        if kind == "console":
            logger.warning("monitor alert occurrence %s rule %s", occurrence.id, rule.id)
            self.session.add(
                AlertDeliveryAttempt(
                    occurrence_id=occurrence.id, destination_type=kind, status="DELIVERED"
                )
            )
        elif kind == "database":
            self.session.add(
                AlertDeliveryAttempt(
                    occurrence_id=occurrence.id, destination_type=kind, status="DELIVERED"
                )
            )
        elif kind == "webhook":
            for attempt_number in range(2):
                try:
                    response = httpx.post(
                        str(rule.destination["url"]),
                        json=body,
                        timeout=3.0,
                        headers={"Idempotency-Key": occurrence.fingerprint},
                    )
                    if response.is_error:
                        self.session.add(
                            AlertDeliveryAttempt(
                                occurrence_id=occurrence.id,
                                destination_type=kind,
                                status="FAILED",
                                response_code=response.status_code,
                                error="HTTPStatusError",
                            )
                        )
                        self.session.flush()
                        if response.status_code >= 500 and attempt_number == 0:
                            continue
                        break
                    self.session.add(
                        AlertDeliveryAttempt(
                            occurrence_id=occurrence.id,
                            destination_type=kind,
                            status="DELIVERED",
                            response_code=response.status_code,
                        )
                    )
                    break
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    self.session.add(
                        AlertDeliveryAttempt(
                            occurrence_id=occurrence.id,
                            destination_type=kind,
                            status="FAILED",
                            error=type(exc).__name__,
                        )
                    )
                    self.session.flush()
                    if attempt_number == 1:
                        break

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    def occurrences(self, project_id: str) -> list[AlertOccurrence]:
        return list(
            self.session.scalars(
                select(AlertOccurrence)
                .where(AlertOccurrence.project_id == project_id)
                .order_by(AlertOccurrence.created_at.desc())
            ).all()
        )

    def resolve(self, occurrence: AlertOccurrence) -> AlertOccurrence:
        occurrence.status = "RESOLVED"
        occurrence.resolved_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(occurrence)
        return occurrence
