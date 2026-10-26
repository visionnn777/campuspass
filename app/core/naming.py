"""Constraint naming convention (spec §2).

Deterministic names mean:
* Alembic diffs stay stable, and
* app/core/db_errors.py can map a violation to an API error by its exact name.

UniqueConstraint("event_id", "user_id") on `registrations` becomes `uq_registrations_event_id`.
CheckConstraint(..., name="capacity") on `events` becomes `ck_events_capacity`.
"""

from sqlalchemy import MetaData

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)
