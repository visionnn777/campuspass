"""registrations table (spec §3, §20): one row per student per event, reused across cycles.

The CHECK constraints protect the consistency of *each row*.
They do not (and cannot) enforce total occupancy; the lock protocol does that (§8).
Hook: constraints guard each seat card; the lock guards the hall door.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pg_enum
from app.models.enums import RegistrationStatus

# FIFO waitlist numbers (§3). Values are taken only while holding the event lock,
# so their order is the true arrival order. Gaps (from rollbacks) are harmless.
WAITLIST_QUEUE_SEQ = Sequence("waitlist_queue_seq", metadata=Base.metadata)


class Registration(TimestampMixin, Base):
    __tablename__ = "registrations"
    __table_args__ = (
        # -> uq_registrations_event_id: at most one row per student per event.
        UniqueConstraint("event_id", "user_id"),
        CheckConstraint(
            "(status = 'waitlisted' AND queue_no IS NOT NULL AND waitlisted_at IS NOT NULL) "
            "OR (status <> 'waitlisted' AND queue_no IS NULL)",
            name="waitlist_fields",
        ),
        CheckConstraint(
            "status NOT IN ('confirmed', 'checked_in') OR confirmed_at IS NOT NULL",
            name="confirmed_fields",
        ),
        CheckConstraint(
            "(status = 'checked_in' AND checked_in_at IS NOT NULL AND checked_in_by IS NOT NULL) "
            "OR (status <> 'checked_in' AND checked_in_at IS NULL AND checked_in_by IS NULL)",
            name="checkin_fields",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) "
            "OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)",
            name="cancel_fields",
        ),
        CheckConstraint("ticket_version >= 1", name="ticket_version"),
        Index("ix_registrations_event_status", "event_id", "status"),
        Index("ix_registrations_user_id", "user_id"),
        Index(
            "ix_registrations_waitlist",
            "event_id",
            "queue_no",
            postgresql_where=text("status = 'waitlisted'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[RegistrationStatus] = mapped_column(
        pg_enum(RegistrationStatus, "registration_status"), nullable=False
    )
    queue_no: Mapped[int | None] = mapped_column(BigInteger)
    ticket_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waitlisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
