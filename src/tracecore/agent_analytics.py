"""Deterministic, data-only agent analytics over stored spans and events."""

import hashlib
import json
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracecore.agent_analytics_schemas import AgentAnalysisConfig, AgentAnalyticsFilters
from tracecore.models import AgentAnalysis, AgentVersion, Run, Span, Trace, TraceEvent, utc_now


class AgentAnalyticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def analyze(self, run_id: str, config: AgentAnalysisConfig | None = None) -> AgentAnalysis:
        run = self.session.get(Run, run_id)
        if run is None:
            raise LookupError(f"Run `{run_id}` was not found")
        config = config or AgentAnalysisConfig()
        trace = self.session.get(Trace, run.trace_id)
        spans = list(
            self.session.scalars(
                select(Span).where(Span.trace_id == run.trace_id).order_by(Span.started_at, Span.id)
            ).all()
        )
        events = list(
            self.session.scalars(
                select(TraceEvent)
                .where(TraceEvent.trace_id == run.trace_id)
                .order_by(TraceEvent.timestamp, TraceEvent.id)
            ).all()
        )
        event_map: dict[str, list[TraceEvent]] = defaultdict(list)
        for event in events:
            if event.span_id:
                event_map[event.span_id].append(event)
        analysis = self._derive(
            run, trace.root_span_id if trace else None, spans, event_map, config
        )
        record = AgentAnalysis(
            run_id=run.id,
            project_id=run.project_id,
            config_snapshot=config.model_dump(),
            analysis=analysis,
            created_at=utc_now(),
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record

    def latest(self, run_id: str) -> AgentAnalysis | None:
        return self.session.scalar(
            select(AgentAnalysis)
            .where(AgentAnalysis.run_id == run_id)
            .order_by(AgentAnalysis.created_at.desc(), AgentAnalysis.id.desc())
        )

    def aggregate(self, filters: AgentAnalyticsFilters) -> dict[str, Any]:
        query = select(Run).where(Run.project_id == filters.project_id)
        if filters.application_id:
            query = query.where(Run.application_id == filters.application_id)
        if filters.agent_name:
            query = query.where(Run.agent_name == filters.agent_name)
        if filters.started_after:
            query = query.where(Run.started_at >= filters.started_after)
        if filters.started_before:
            query = query.where(Run.started_at <= filters.started_before)
        runs = self.session.scalars(query.order_by(Run.started_at)).all()
        run_ids = [item.id for item in runs]
        version_ids = {run.agent_version_id for run in runs if run.agent_version_id}
        versions = (
            {
                item.id: item.version
                for item in self.session.scalars(
                    select(AgentVersion).where(AgentVersion.id.in_(version_ids))
                ).all()
            }
            if version_ids
            else {}
        )
        if filters.agent_version:
            runs = [
                run
                for run in runs
                if run.agent_version_id is not None
                and versions.get(run.agent_version_id) == filters.agent_version
            ]
            run_ids = [item.id for item in runs]
        spans_by_trace: dict[str, list[Span]] = defaultdict(list)
        events_by_trace_span: dict[str, dict[str, list[TraceEvent]]] = defaultdict(
            lambda: defaultdict(list)
        )
        if runs:
            traces = {item.trace_id for item in runs}
            spans = self.session.scalars(
                select(Span).where(Span.trace_id.in_(traces)).order_by(Span.started_at, Span.id)
            ).all()
            for span in spans:
                spans_by_trace[span.trace_id].append(span)
            trace_events = self.session.scalars(
                select(TraceEvent)
                .where(TraceEvent.trace_id.in_(traces))
                .order_by(TraceEvent.timestamp)
            ).all()
            for event in trace_events:
                if event.span_id:
                    events_by_trace_span[event.trace_id][event.span_id].append(event)
        grouped: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
        for run in runs:
            version = versions.get(run.agent_version_id) if run.agent_version_id else None
            key = (run.application_id, run.agent_name, version)
            group = grouped.setdefault(
                key,
                {
                    "application_id": run.application_id,
                    "agent_name": run.agent_name,
                    "agent_version": version,
                    "run_count": 0,
                    "tool_usage": {},
                    "explicit_retry_runs": 0,
                    "retry_count": 0,
                    "handoff_count": 0,
                    "handoff_runs": 0,
                    "tool_usage_status": "UNAVAILABLE",
                    "tool_observed_runs": 0,
                },
            )
            group["run_count"] += 1
            run_spans = spans_by_trace.get(run.trace_id, [])
            if any(span.span_type == "TOOL" for span in run_spans):
                group["tool_observed_runs"] += 1
            run_events = events_by_trace_span.get(run.trace_id, {})
            retry_count, retry_observed, _ = self._retry_data(run_spans, run_events)
            if retry_observed:
                group["explicit_retry_runs"] += 1
                group["retry_count"] += retry_count
            handoffs, handoffs_observed = self._handoffs(run_spans, run_events)
            if handoffs_observed:
                group["handoff_runs"] += 1
            group["handoff_count"] += len(handoffs)
            for span in run_spans:
                if span.span_type != "TOOL":
                    continue
                tool = group["tool_usage"].setdefault(
                    span.name,
                    {"calls": 0, "succeeded": 0, "failed": 0, "outcome_unavailable": 0},
                )
                group["tool_usage_status"] = "AVAILABLE"
                tool["calls"] += 1
                outcome = self._tool_outcome(span)
                if outcome == "SUCCEEDED":
                    tool["succeeded"] += 1
                elif outcome == "FAILED":
                    tool["failed"] += 1
                else:
                    tool["outcome_unavailable"] += 1
        output_groups = []
        for key in sorted(grouped, key=lambda value: tuple(item or "" for item in value)):
            group = grouped[key]
            group["tool_usage_status"] = (
                "UNAVAILABLE"
                if not group["tool_observed_runs"]
                else "AVAILABLE"
                if group["tool_observed_runs"] == group["run_count"]
                else "PARTIAL"
            )
            group["tool_usage"] = [
                {"tool_name": name, **values}
                for name, values in sorted(group["tool_usage"].items())
            ]
            group["retry_status"] = (
                "UNAVAILABLE"
                if not group["explicit_retry_runs"]
                else "AVAILABLE"
                if group["explicit_retry_runs"] == group["run_count"]
                else "PARTIAL"
            )
            if group["retry_status"] != "AVAILABLE":
                group["retry_count"] = None
            group["handoff_status"] = (
                "UNAVAILABLE"
                if not group["handoff_runs"]
                else "AVAILABLE"
                if group["handoff_runs"] == group["run_count"]
                else "PARTIAL"
            )
            if group["handoff_status"] != "AVAILABLE":
                group["handoff_count"] = None
            output_groups.append(group)
        return {
            "project_id": filters.project_id,
            "filters": filters.model_dump(mode="json"),
            "run_count": len(run_ids),
            "groups": output_groups,
        }

    @classmethod
    def _derive(
        cls,
        run: Run,
        root_span_id: str | None,
        spans: list[Span],
        events_by_span: dict[str, list[TraceEvent]],
        config: AgentAnalysisConfig,
    ) -> dict[str, Any]:
        tool_groups: dict[str, dict[str, Any]] = {}
        tool_spans = [span for span in spans if span.span_type == "TOOL"]
        for span in tool_spans:
            counts = tool_groups.setdefault(
                span.name,
                {"calls": 0, "succeeded": 0, "failed": 0, "outcome_unavailable": 0, "span_ids": []},
            )
            counts["calls"] += 1
            counts["span_ids"].append(span.id)
            outcome = cls._tool_outcome(span)
            counts[
                "succeeded"
                if outcome == "SUCCEEDED"
                else "failed"
                if outcome == "FAILED"
                else "outcome_unavailable"
            ] += 1
        retry_count, retry_available, retry_evidence = cls._retry_data(spans, events_by_span)
        handoffs, handoffs_available = cls._handoffs(spans, events_by_span)
        loop_events, step_count, step_count_status = cls._loops(
            spans, root_span_id, events_by_span, config
        )
        return {
            "run_id": run.id,
            "trace_id": run.trace_id,
            "application_id": run.application_id,
            "agent_name": run.agent_name,
            "agent_version_id": run.agent_version_id,
            "run_status": run.status,
            "tool_usage_status": "AVAILABLE" if tool_spans else "UNAVAILABLE",
            "tool_usage": [
                {"tool_name": name, **counts} for name, counts in sorted(tool_groups.items())
            ],
            "retry_analysis": {
                "status": "AVAILABLE" if retry_available else "UNAVAILABLE",
                "count": retry_count if retry_available else None,
                "evidence": retry_evidence,
            },
            "handoffs": {
                "status": "AVAILABLE" if handoffs_available else "UNAVAILABLE",
                "relationships": handoffs,
            },
            "loop_detection": {
                "status": "AVAILABLE" if spans else "UNAVAILABLE",
                "step_count": step_count,
                "step_count_status": step_count_status,
                "events": loop_events,
            },
        }

    @staticmethod
    def _tool_outcome(span: Span) -> str:
        if span.status == "ERROR" or span.error:
            return "FAILED"
        if span.status == "OK":
            return "SUCCEEDED"
        return "UNAVAILABLE"

    @staticmethod
    def _retry_data(
        spans: list[Span], events_by_span: dict[str, list[TraceEvent]]
    ) -> tuple[int, bool, list[dict[str, Any]]]:
        total = 0
        observed = False
        evidence: list[dict[str, Any]] = []
        for span in spans:
            attrs = span.attributes or {}
            count: int | None = None
            source = ""
            if isinstance(attrs.get("retry_count"), int) and not isinstance(
                attrs.get("retry_count"), bool
            ):
                count, source = max(0, attrs["retry_count"]), "retry_count"
            elif isinstance(attrs.get("retry_attempt"), int) and not isinstance(
                attrs.get("retry_attempt"), bool
            ):
                count, source = max(0, attrs["retry_attempt"] - 1), "retry_attempt"
            elif isinstance(attrs.get("attempt"), int) and not isinstance(
                attrs.get("attempt"), bool
            ):
                count, source = max(0, attrs["attempt"] - 1), "attempt"
            elif attrs.get("retry") is True:
                count, source = 1, "retry"
            retry_events = [
                event
                for event in events_by_span.get(span.id, [])
                if event.name.lower() == "retry" or event.name.lower().endswith(".retry")
            ]
            if count is not None or retry_events:
                observed = True
                explicit_count = max(count or 0, len(retry_events))
                total += explicit_count
                evidence.append(
                    {
                        "span_id": span.id,
                        "count": explicit_count,
                        "source": source or "recorded_event",
                        "event_names": [event.name for event in retry_events],
                    }
                )
        return total, observed, evidence

    @classmethod
    def _handoffs(
        cls, spans: list[Span], events_by_span: dict[str, list[TraceEvent]]
    ) -> tuple[list[dict[str, Any]], bool]:
        found: list[dict[str, Any]] = []
        for span in spans:
            attrs = span.attributes or {}
            source = (
                attrs.get("source_agent") or attrs.get("parent_agent") or attrs.get("handoff_from")
            )
            target = attrs.get("target_agent") or attrs.get("handoff_to")
            if isinstance(source, str) or isinstance(target, str):
                found.append(
                    {
                        "from_agent": source if isinstance(source, str) else None,
                        "to_agent": target if isinstance(target, str) else None,
                        "span_id": span.id,
                        "source": "span_attributes",
                    }
                )
            for event in events_by_span.get(span.id, []):
                if event.name.lower() not in {"handoff", "agent.handoff", "agent_handoff"}:
                    continue
                event_attrs = event.attributes or {}
                found.append(
                    {
                        "from_agent": event_attrs.get("source_agent")
                        or event_attrs.get("from_agent"),
                        "to_agent": event_attrs.get("target_agent") or event_attrs.get("to_agent"),
                        "span_id": span.id,
                        "event_name": event.name,
                        "source": "recorded_event",
                    }
                )
        return found, bool(found)

    @classmethod
    def _loops(
        cls,
        spans: list[Span],
        root_span_id: str | None,
        events_by_span: dict[str, list[TraceEvent]],
        config: AgentAnalysisConfig,
    ) -> tuple[list[dict[str, Any]], int, str]:
        events: list[dict[str, Any]] = []
        child_spans = [span for span in spans if span.id != root_span_id]
        step_count = len(child_spans)
        if step_count >= config.excessive_step_count_threshold:
            events.append(
                {
                    "pattern": "EXCESSIVE_STEP_COUNT",
                    "severity": config.excessive_step_count_severity,
                    "count": step_count,
                    "threshold": config.excessive_step_count_threshold,
                    "span_ids": [span.id for span in child_spans],
                    "detection_method": "TRACE_STRUCTURE",
                }
            )

        consecutive: list[Span] = []
        for span in [item for item in spans if item.span_type == "TOOL"]:
            if consecutive and consecutive[-1].name != span.name:
                cls._append_repeated_tool_event(events, consecutive, config)
                consecutive = []
            consecutive.append(span)
        cls._append_repeated_tool_event(events, consecutive, config)

        failures: dict[tuple[str, str], list[Span]] = defaultdict(list)
        for span in spans:
            if span.status == "ERROR" or span.error:
                signature = cls._fingerprint(span.error or span.status)
                failures[(span.name, signature)].append(span)
        for (name, fingerprint), items in failures.items():
            if len(items) >= config.repeated_failure_threshold:
                events.append(
                    {
                        "pattern": "REPEATED_FAILURE",
                        "severity": config.repeated_failure_severity,
                        "span_name": name,
                        "error_fingerprint": fingerprint,
                        "count": len(items),
                        "threshold": config.repeated_failure_threshold,
                        "span_ids": [span.id for span in items],
                        "detection_method": "EXACT_ERROR_HEURISTIC",
                    }
                )

        plan_groups: dict[tuple[str, str], list[Span]] = defaultdict(list)
        plan_methods: dict[tuple[str, str], str] = {}
        for span in spans:
            if span.span_type != "PLANNER":
                continue
            attrs = span.attributes or {}
            explicit = attrs.get("plan_id") or attrs.get("plan_hash")
            plan = explicit if explicit is not None else span.output_reference
            if plan is None:
                continue
            fingerprint = cls._fingerprint(plan)
            group_key = ("explicit" if explicit is not None else "heuristic", fingerprint)
            plan_groups[group_key].append(span)
            plan_methods[group_key] = (
                "EXPLICIT_PLAN_ID" if explicit is not None else "IDENTICAL_PLAN_OUTPUT_HEURISTIC"
            )
        for group_key, items in plan_groups.items():
            if len(items) >= config.repeated_plan_threshold:
                events.append(
                    {
                        "pattern": "REPEATED_PLAN",
                        "severity": config.repeated_plan_severity,
                        "count": len(items),
                        "threshold": config.repeated_plan_threshold,
                        "span_ids": [span.id for span in items],
                        "detection_method": plan_methods[group_key],
                        "fingerprint": group_key[1],
                    }
                )
        return events, step_count, "AVAILABLE" if spans else "UNAVAILABLE"

    @classmethod
    def _append_repeated_tool_event(
        cls, events: list[dict[str, Any]], spans: list[Span], config: AgentAnalysisConfig
    ) -> None:
        if len(spans) >= config.repeated_tool_call_threshold:
            events.append(
                {
                    "pattern": "REPEATED_TOOL_CALL",
                    "tool_name": spans[0].name,
                    "severity": config.repeated_tool_call_severity,
                    "count": len(spans),
                    "threshold": config.repeated_tool_call_threshold,
                    "span_ids": [span.id for span in spans],
                    "detection_method": "CONSECUTIVE_TOOL_NAME_HEURISTIC",
                }
            )

    @staticmethod
    def _fingerprint(value: Any) -> str:
        serialized = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]
