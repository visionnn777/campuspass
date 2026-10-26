"""Admin schemas (spec §5, §19)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from app.models import UserRole


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: UserRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_and_no_nulls(self) -> AdminUserUpdate:
        if not self.model_fields_set:
            raise ValueError("provide at least one of: role, is_active")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self
