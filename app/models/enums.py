"""Enumerations shared by models and schemas.

StrEnum values are what PostgreSQL stores in the native ENUM types (spec §20).
"""

from enum import StrEnum


class UserRole(StrEnum):
    STUDENT = "student"
    ORGANISER = "organiser"
    ADMIN = "admin"


class EventCategory(StrEnum):
    TECH = "tech"
    CULTURAL = "cultural"
    SPORTS = "sports"
    WORKSHOP = "workshop"
    OTHER = "other"


class EventStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class RegistrationStatus(StrEnum):
    CONFIRMED = "confirmed"
    WAITLISTED = "waitlisted"
    CANCELLED = "cancelled"
    CHECKED_IN = "checked_in"


# Statuses that hold (or are waiting for) a seat. Used by PUT to detect "already registered".
ACTIVE_REGISTRATION_STATUSES = frozenset(
    {RegistrationStatus.CONFIRMED, RegistrationStatus.WAITLISTED, RegistrationStatus.CHECKED_IN}
)
# Statuses that occupy a seat (the capacity invariant counts these).
OCCUPYING_STATUSES = (RegistrationStatus.CONFIRMED, RegistrationStatus.CHECKED_IN)
