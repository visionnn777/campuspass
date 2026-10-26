"""Declarative base and shared column helpers."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.naming import metadata


class Base(DeclarativeBase):
    metadata = metadata


def pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """A native PostgreSQL ENUM that stores the lowercase *values* ('student'), not names."""
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class TimestampMixin:
    """created_at / updated_at bookkeeping columns.

    These are audit columns, not business decisions, so database time is fine here.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
