"""Deterministic governance checks over persisted run telemetry."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracecore.governance_schemas import PolicyCreate, ViolationTransitionRequest
from tracecore.models import (
    AgentVersion,
    GovernancePolicy,
    PolicyEvaluation,
    PolicyTransition,
    PolicyViolation,
    Project,
    Run,
    Span,
)
from tracecore.redaction import BasicRedactionService


class GovernanceService:
    """Create immutable policy versions and evaluate them against stored traces."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_policy(self, request: PolicyCreate) -> GovernancePolicy:
        if self.session.get(Project, request.project_id) is None:
            raise LookupError(f"Project `{request.project_id}` was not found")
        latest = self.session.scalar(
            select(GovernancePolicy)
            .where(
                GovernancePolicy.project_id == request.project_id,
                GovernancePolicy.name == request.name,
            )
            .order_by(GovernancePolicy.version.desc())
            .limit(1)
        )
        redactor = BasicRedactionService()
        policy = GovernancePolicy(
            project_id=request.project_id,
            name=request.name,
            version=(latest.version + 1) if latest else 1,
            description=request.description,
            conditions=redactor.redact([item.model_dump() for item in request.conditions]),
            effect=request.effect,
            severity=request.severity,
        )
        self.session.add(policy)
        self.session.flush()
        return policy

    def evaluate(
        self, run_id: str, policy_ids: list[str] | None = None
    ) -> list[tuple[PolicyEvaluation, PolicyViolation | None]]:
        run = self.session.get(Run, run_id)
        if run is None:
            raise LookupError(f"Run `{run_id}` was not found")
        query = select(GovernancePolicy).where(GovernancePolicy.project_id == run.project_id)
        if policy_ids is not None:
            query = query.where(GovernancePolicy.id.in_(policy_ids))
            policies = self.session.scalars(query.order_by(GovernancePolicy.name)).all()
            found_ids = {item.id for item in policies}
            if found_ids != set(policy_ids):
                raise ValueError("One or more policies were not found in the run's project")
        else:
            policies = self.session.scalars(query.order_by(GovernancePolicy.name)).all()
        spans = self.session.scalars(
            select(Span).where(Span.trace_id == run.trace_id).order_by(Span.started_at)
        ).all()
        evidence_values = self._values(run, spans)
        output: list[tuple[PolicyEvaluation, PolicyViolation | None]] = []
        for policy in policies:
            policy_snapshot = {
                "id": policy.id,
                "project_id": policy.project_id,
                "name": policy.name,
                "version": policy.version,
                "description": policy.description,
                "conditions": policy.conditions,
                "effect": policy.effect,
                "severity": policy.severity,
            }
            condition_results: list[dict[str, Any]] = []
            for condition in policy.conditions:
                field_name = condition["field"]
                available, observed = evidence_values[field_name]
                operator = condition["operator"]
                expected = condition.get("value")
                status = self._condition_status(available, observed, operator, expected)
                condition_results.append(
                    {
                        "field": field_name,
                        "operator": operator,
                        "status": status,
                        "expected": expected,
                        "observed": observed if available else None,
                        "available": available,
                    }
                )
            statuses = {item["status"] for item in condition_results}
            result = (
                "NO_MATCH"
                if "NO_MATCH" in statuses
                else ("UNAVAILABLE" if "UNAVAILABLE" in statuses else "MATCH")
            )
            decision = policy.effect if result == "MATCH" else result
            evaluation = PolicyEvaluation(
                run_id=run.id,
                project_id=run.project_id,
                policy_id=policy.id,
                policy_version=policy.version,
                policy_snapshot=policy_snapshot,
                result=result,
                decision=decision,
                evidence=condition_results,
            )
            self.session.add(evaluation)
            self.session.flush()
            violation: PolicyViolation | None = None
            if result == "MATCH" and policy.effect != "ALLOW":
                violation = PolicyViolation(
                    policy_evaluation_id=evaluation.id,
                    run_id=run.id,
                    project_id=run.project_id,
                    policy_id=policy.id,
                    policy_version=policy.version,
                    policy_snapshot=policy_snapshot,
                    effect=policy.effect,
                    severity=policy.severity,
                    evidence=condition_results,
                    status="OPEN",
                    review_required=policy.effect == "REQUIRE_REVIEW",
                )
                self.session.add(violation)
                self.session.flush()
                self.session.add(
                    PolicyTransition(
                        violation_id=violation.id,
                        from_status=None,
                        to_status="OPEN",
                        actor="system",
                        reason="Policy condition matched stored run telemetry",
                    )
                )
            output.append((evaluation, violation))
        self.session.flush()
        return output

    def transition(
        self, violation: PolicyViolation, request: ViolationTransitionRequest
    ) -> PolicyTransition:
        now = datetime.now(UTC)
        expires_at = request.expires_at
        if expires_at is not None and expires_at <= now:
            raise ValueError("waiver expiration must be in the future")
        previous = violation.status
        transition = PolicyTransition(
            violation_id=violation.id,
            from_status=previous,
            to_status=request.status,
            actor=request.actor,
            reason=request.reason,
            expires_at=expires_at,
        )
        self.session.add(transition)
        violation.status = request.status
        if request.status == "WAIVED":
            violation.waiver_actor = request.actor
            violation.waiver_reason = request.reason
            violation.waived_at = now
            violation.waiver_expires_at = expires_at
        else:
            violation.waiver_actor = None
            violation.waiver_reason = None
            violation.waived_at = None
            violation.waiver_expires_at = None
        self.session.flush()
        return transition

    @staticmethod
    def _condition_status(
        available: bool, observed: list[Any], operator: str, expected: Any
    ) -> str:
        if not available:
            return "UNAVAILABLE"
        present = bool(observed)
        if operator == "exists":
            return "MATCH" if present else "NO_MATCH"
        if operator == "not_exists":
            return "MATCH" if not present else "NO_MATCH"
        expected_values = expected if operator in {"in", "not_in"} else [expected]
        if operator in {"eq", "in"}:
            matched = any(value in expected_values for value in observed)
        else:
            matched = any(value not in expected_values for value in observed)
        return "MATCH" if matched else "NO_MATCH"

    def _values(self, run: Run, spans: Sequence[Span]) -> dict[str, tuple[bool, list[Any]]]:
        version = (
            self.session.get(AgentVersion, run.agent_version_id) if run.agent_version_id else None
        )
        model_values: list[Any] = []
        prompt_values: list[Any] = []
        span_statuses: list[Any] = []
        tool_names: list[Any] = []
        for span in spans:
            if span.status:
                span_statuses.append(span.status)
            if span.span_type.upper() == "TOOL":
                tool_names.append(span.name)
            if span.span_type.upper() == "LLM":
                for key in ("model", "model_name", "llm.model", "gen_ai.request.model"):
                    if span.attributes.get(key) is not None:
                        model_values.append(span.attributes[key])
                for key in ("prompt_version", "prompt.version"):
                    if span.attributes.get(key) is not None:
                        prompt_values.append(span.attributes[key])
        for item in run.model_usage.values() if isinstance(run.model_usage, dict) else []:
            if isinstance(item, dict) and item.get("model") is not None:
                model_values.append(item["model"])
        return {
            "environment": (True, [run.environment]),
            "agent_name": (True, [run.agent_name] if run.agent_name else []),
            "agent_version": (True, [version.version] if version else []),
            "run_status": (True, [run.status]),
            "logging_mode": (
                "logging_mode" in (run.metadata_json or {}),
                [run.metadata_json["logging_mode"]]
                if "logging_mode" in (run.metadata_json or {})
                else [],
            ),
            "model": (bool(model_values), model_values),
            "prompt_version": (bool(prompt_values), prompt_values),
            "span_status": (bool(span_statuses), span_statuses),
            "tool_name": (bool(tool_names), tool_names),
        }
