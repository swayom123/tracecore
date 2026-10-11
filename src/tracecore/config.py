import json
import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRACECORE_", env_file=".env", extra="ignore")

    data_mode: Literal["live", "demo", "test"] = "live"
    database_url: str = "sqlite:///./tracecore.db"
    max_payload_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    logging_mode: Literal["METADATA_ONLY", "REDACTED", "FULL_CONTENT"] = "REDACTED"
    log_level: str = "INFO"
    evaluation_allowed_providers: str = ""
    evaluation_restricted_data_providers: str = ""
    evaluation_openai_base_url: str = "https://api.openai.com/v1"
    evaluation_openai_api_key: str | None = None
    monitoring_percentile_min_samples: int = Field(default=20, ge=1, le=1000000)
    runtime_environment: Literal["development", "staging", "production"] = "development"
    auth_mode: Literal["development", "bearer"] = "development"
    api_tokens: str = ""
    rate_limit_per_minute: int = Field(default=600, ge=1, le=100000)
    retention_days_by_environment: str = '{"development":30,"staging":60,"production":90}'
    redaction_patterns: str = ""

    @field_validator("redaction_patterns")
    @classmethod
    def validate_redaction_patterns(cls, value: str) -> str:
        if not value:
            return value
        try:
            patterns = json.loads(value)
        except ValueError as exc:
            raise ValueError("redaction_patterns must be a JSON array of regex strings") from exc
        if (
            not isinstance(patterns, list)
            or len(patterns) > 100
            or any(
                not isinstance(pattern, str) or not pattern or len(pattern) > 512
                for pattern in patterns
            )
        ):
            raise ValueError(
                "redaction_patterns must contain at most 100 regex strings of up to 512 characters"
            )
        try:
            for pattern in patterns:
                re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid configured redaction regex: {exc}") from exc
        return value

    @field_validator("retention_days_by_environment")
    @classmethod
    def validate_retention_days(cls, value: str) -> str:
        try:
            policies = json.loads(value)
        except ValueError as exc:
            raise ValueError("retention_days_by_environment must be a JSON object") from exc
        if (
            not isinstance(policies, dict)
            or not policies
            or any(
                not isinstance(environment, str)
                or not environment
                or isinstance(days, bool)
                or not isinstance(days, int)
                or days < 1
                for environment, days in policies.items()
            )
        ):
            raise ValueError(
                "retention_days_by_environment must map environment names to positive day counts"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
