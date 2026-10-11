"""Contracts for operational credential administration."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    role: Literal["ADMIN", "PLATFORM_ENGINEER", "AI_ENGINEER", "EVALUATOR", "AUDITOR", "VIEWER"]
    organization_id: str = Field(min_length=1, max_length=64)
    project_ids: list[str] = Field(min_length=1, max_length=200)
    environments: list[str] = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None

    @field_validator("project_ids", "environments")
    @classmethod
    def nonempty_unique_values(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("scope values must be non-empty and unique")
        return value

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at must include a timezone")
        return value
