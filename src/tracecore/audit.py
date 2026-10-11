"""Append-only audit chain helpers; event details must never contain trace payloads."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tracecore.models import AuditEvent
from tracecore.security import Principal

ZERO_HASH = "0" * 64


def _canonical(event: dict[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode()


def _entry_hash(event: AuditEvent, previous_hash: str) -> str:
    created_at = event.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = created_at.astimezone(UTC)
    return hashlib.sha256(
        _canonical(
            {
                "organization_id": event.organization_id,
                "project_id": event.project_id,
                "actor": event.actor,
                "action": event.action,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "details": event.details,
                "previous_hash": previous_hash,
                "created_at": created_at.isoformat(),
            }
        )
    ).hexdigest()


def append_audit(
    session: Session,
    principal: Principal,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
    project_id: str | None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    if not principal.organization_id:
        raise ValueError("Audit events require an organization-scoped principal")
    if session.bind and session.bind.dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(principal.organization_id.encode()).digest()[:8], "big", signed=True
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:audit_key)"), {"audit_key": key})
    was_skipped = session.info.get("skip_scope", False)
    session.info["skip_scope"] = True
    try:
        previous = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.organization_id == principal.organization_id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        previous_hash = previous.entry_hash if previous else ZERO_HASH
        created_at = datetime.now(UTC)
        if previous:
            previous_created = previous.created_at
            if previous_created.tzinfo is None:
                previous_created = previous_created.replace(tzinfo=UTC)
            if previous_created >= created_at:
                created_at = previous_created + timedelta(microseconds=1)
        row = AuditEvent(
            organization_id=principal.organization_id,
            project_id=project_id,
            actor=principal.subject,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
            previous_hash=previous_hash,
            entry_hash="",
            created_at=created_at,
        )
        row.entry_hash = _entry_hash(row, previous_hash)
        session.add(row)
        session.flush()
        return row
    finally:
        session.info["skip_scope"] = was_skipped


def verify_audit(session: Session, organization_id: str) -> dict[str, Any]:
    was_skipped = session.info.get("skip_scope", False)
    session.info["skip_scope"] = True
    try:
        rows = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.organization_id == organization_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
    finally:
        session.info["skip_scope"] = was_skipped
    previous = ZERO_HASH
    for index, row in enumerate(rows):
        if row.previous_hash != previous or row.entry_hash != _entry_hash(row, previous):
            return {
                "valid": False,
                "checked": index + 1,
                "event_id": row.id,
                "head_hash": previous,
                "organization_id": organization_id,
            }
        previous = row.entry_hash
    return {
        "valid": True,
        "checked": len(rows),
        "event_id": None,
        "head_hash": previous,
        "organization_id": organization_id,
    }
