"""Redaction helpers shared by the ingestion service and the Python SDK."""

import json
import re
from typing import Any


class BasicRedactionService:
    _secret_patterns = (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\b\s*[:=]\s*[^\s,;]+"
        ),
        re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"),
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    )
    _sensitive_key = re.compile(
        r"(?i)(password|secret|api[_-]?key|token|authorization|credential|private[_-]?key|customer|personal|ssn|social[_-]?security|date[_-]?of[_-]?birth|passport)"
    )

    def __init__(self, custom_patterns: str = "") -> None:
        self.custom_patterns: tuple[re.Pattern[str], ...] = ()
        if custom_patterns:
            try:
                values = json.loads(custom_patterns)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "redaction_patterns must be a JSON array of regex strings"
                ) from exc
            if (
                not isinstance(values, list)
                or len(values) > 100
                or any(
                    not isinstance(value, str) or not value or len(value) > 512 for value in values
                )
            ):
                raise ValueError(
                    "redaction_patterns must contain at most 100 non-empty regex strings up to 512 characters"
                )
            try:
                self.custom_patterns = tuple(re.compile(value) for value in values)
            except re.error as exc:
                raise ValueError(f"Invalid configured redaction regex: {exc}") from exc

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]"
                if self._sensitive_key.search(str(key))
                else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            redacted = value
            for pattern in (*self._secret_patterns, *self.custom_patterns):
                redacted = pattern.sub("[REDACTED]", redacted)
            return redacted
        return value


class MetadataOnlyRedactionService:
    def redact(self, value: Any) -> Any:
        if value is None:
            return None
        return {"content_omitted": True, "content_type": type(value).__name__}
