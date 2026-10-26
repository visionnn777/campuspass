"""events table (spec §3, §20).

"Completed", "registration open", seats left and waitlist count are *derived*,
never stored, so they can never go stale.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pg_enum
from app.models.enums import EventCategory, EventStatus


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("capacity BETWEEN 1 AND 10000", name="capacity"),
        CheckConstraint("waitlist_limit BETWEEN 0 AND 10000", name="waitlist_limit"),
        CheckConstraint("char_length(description) <= 5000", name="description"),
        CheckConstraint(
            "registration_opens_at < registration_closes_at "
            "AND registration_closes_at <= starts_at "
            "AND starts_at < ends_at",
            name="time_order",
        ),
        # cancelled_at is set exactly when the event is cancelled.
        CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="cancelled_at"),
        Index("ix_events_status_starts_at", "status", "starts_at"),
        Index("ix_events_category", "category"),
        Index("ix_events_organiser_id", "organiser_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Default ondelete is RESTRICT/NO ACTION: history is never silently deleted.
    organiser_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    category: Mapped[EventCategory] = mapped_column(
        pg_enum(EventCategory, "event_category"), nullable=False
    )
    venue: Mapped[str] = mapped_column(String(200), nullable=False)
    registration_opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registration_closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    waitlist_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )
    status: Mapped[EventStatus] = mapped_column(
        pg_enum(EventStatus, "event_status"),
        nullable=False,
        default=EventStatus.DRAFT,
        server_default=text("'draft'"),
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
