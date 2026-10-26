"""Importing this package registers every table on Base.metadata (used by Alembic)."""

from app.models.base import Base
from app.models.enums import EventCategory, EventStatus, RegistrationStatus, UserRole
from app.models.event import Event
from app.models.registration import WAITLIST_QUEUE_SEQ, Registration
from app.models.user import User

__all__ = [
    "WAITLIST_QUEUE_SEQ",
    "Base",
    "Event",
    "EventCategory",
    "EventStatus",
    "Registration",
    "RegistrationStatus",
    "User",
    "UserRole",
]
