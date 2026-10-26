"""Auth schemas (spec §5, §19). Every input uses extra="forbid" (mass-assignment protection)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from app.core.config import get_settings
from app.core.security import PASSWORD_MAX_LENGTH, password_policy_issues
from app.models import UserRole

FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def normalize_email(value: str) -> str:
    return value.strip().lower()


class SignupIn(BaseModel):
    # Sending "role", "is_active" or "id" is a 422: clients cannot choose their privileges.
    model_config = ConfigDict(extra="forbid")

    # Field order matters: password is validated last, so it can see email and full_name.
    email: EmailStr = Field(max_length=254)
    full_name: FullName
    password: str = Field(max_length=PASSWORD_MAX_LENGTH * 2)  # exact policy checked below

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        email = normalize_email(value)
        allowed = get_settings().allowed_domains
        if allowed and email.rsplit("@", 1)[-1] not in allowed:
            raise ValueError("this email domain is not allowed to sign up")
        return email

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: str, info: ValidationInfo) -> str:
        issues = password_policy_issues(
            value, email=info.data.get("email", ""), full_name=info.data.get("full_name", "")
        )
        if issues:
            raise ValueError("password " + "; ".join(issues))
        return value


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH * 2)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(BaseModel):
    """Never includes password_hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
