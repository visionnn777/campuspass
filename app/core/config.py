"""Application settings, loaded from environment variables and `.env` (spec §15).

Every value is validated at startup. If anything is missing or unsafe the app
refuses to start, which is better than running with a weak secret.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 64  # secrets.token_urlsafe(48) produces exactly 64 characters


class Settings(BaseSettings):
    """Typed configuration. Environment variables override values in `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"

    database_url: str
    # Only the test suite reads this (through tests/db_guard.py). Never used as a fallback.
    test_database_url: str | None = None

    jwt_secret: str
    jwt_issuer: str = "campuspass"
    jwt_audience: str = "campuspass-api"
    access_token_minutes: int = Field(default=30, ge=1, le=1440)

    ticket_secret: str

    rate_limits_enabled: bool = True
    # Comma-separated list, e.g. "ipu.ac.in,college.edu". Empty = any domain allowed.
    allowed_email_domains: str = ""

    max_body_bytes: int = Field(default=65536, ge=1024, le=10 * 1024 * 1024)

    db_lock_timeout_ms: int = Field(default=3000, ge=100, le=60000)
    db_statement_timeout_ms: int = Field(default=10000, ge=500, le=300000)
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)

    @field_validator("jwt_secret", "ticket_secret")
    @classmethod
    def _secret_must_be_strong(cls, value: str) -> str:
        if "CHANGE_ME" in value:
            raise ValueError("is still the placeholder from .env.example; generate a real secret")
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_LENGTH} characters "
                "(generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def _postgres_only(cls, value: str) -> str:
        # Row locks, CHECK constraints and sequences are the core of this project: PostgreSQL only.
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("must start with postgresql+psycopg:// (PostgreSQL via psycopg 3)")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Settings:
        # Key separation (spec §11): one leaked key must not break both login and tickets.
        if self.jwt_secret == self.ticket_secret:
            raise ValueError("JWT_SECRET and TICKET_SECRET must be different")
        # Rate limits may only be switched off for the automated test suite (spec §6).
        if not self.rate_limits_enabled and self.app_env != "test":
            raise ValueError("RATE_LIMITS_ENABLED=false is only allowed when APP_ENV=test")
        return self

    @property
    def allowed_domains(self) -> frozenset[str]:
        parts = (p.strip().lower() for p in self.allowed_email_domains.split(","))
        return frozenset(p for p in parts if p)


@lru_cache
def get_settings() -> Settings:
    """One Settings object per process (cached)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
