"""The lock protocol helpers (spec §8). THE most important file to explain.

Event-state operations lock the event before any registration row. Operations that
also need to validate a user's active status lock the user row only AFTER the event
and registration locks. Multiple user rows are always locked in ascending UUID order.

Rule 1  Event first, always: lock the event row, then registration rows, then users.
Rule 2  FOR UPDATE for state-changing operations; FOR SHARE for ticket reissue/check-in
        and for reading user eligibility while holding a registration lock. Never upgrade.
Rule 3  Read the business clock and run counts AFTER the required locks are held.
Rule 4  lock_timeout / statement_timeout are set on every connection (database.py).
Rule 5  No hashing, QR rendering or network I/O while a lock is held.

The event lock serializes capacity-changing operations for one event. The row-level
constraints independently protect registration-row integrity and uniqueness.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Event, Registration, RegistrationStatus, User
from app.models.enums import OCCUPYING_STATUSES


def lock_event_for_update(db: Session, event_id: uuid.UUID) -> Event | None:
    """Exclusive event lock. Blocks writers and shared lockers for this event."""
    stmt = (
        select(Event)
        .where(Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one_or_none()


def lock_event_for_share(db: Session, event_id: uuid.UUID) -> Event | None:
    """Shared event lock: parallel check-ins/reissues, serialized against event changes."""
    stmt = (
        select(Event)
        .where(Event.id == event_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one_or_none()


def lock_registration_of_user(db: Session, event_id: uuid.UUID, user_id: uuid.UUID) -> Registration | None:
    """Lock a participant row only while the event lock is already held."""
    stmt = (
        select(Registration)
        .where(Registration.event_id == event_id, Registration.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one_or_none()


def lock_registration_by_id(
    db: Session, event_id: uuid.UUID, registration_id: uuid.UUID
) -> Registration | None:
    """Lock a registration and bind it to the event in the same query."""
    stmt = (
        select(Registration)
        .where(Registration.id == registration_id, Registration.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one_or_none()


def lock_users_for_share(db: Session, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    """Lock and refresh several users in deterministic UUID order.

    Call only after event and registration locks. FOR SHARE conflicts with the UPDATE
    used by account deactivation and role changes. If a change commits first, the query
    returns the fresh state; if these locks win first, the admin change waits until this
    already-authorized operation commits. `populate_existing` prevents stale identity-map
    values from being treated as current authorization data.
    """
    ordered_ids = sorted(user_ids, key=str)
    if not ordered_ids:
        return {}
    stmt = (
        select(User)
        .where(User.id.in_(ordered_ids))
        .order_by(User.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return {user.id: user for user in db.scalars(stmt).all()}


def lock_user_for_share(db: Session, user_id: uuid.UUID) -> User | None:
    """Convenience wrapper for the common single-user eligibility check."""
    return lock_users_for_share(db, {user_id}).get(user_id)


def count_occupancy(db: Session, event_id: uuid.UUID) -> tuple[int, int]:
    """Return (occupancy, waiting). Call ONLY after holding the event FOR UPDATE lock."""
    occupying = func.count().filter(Registration.status.in_(OCCUPYING_STATUSES))
    waiting = func.count().filter(Registration.status == RegistrationStatus.WAITLISTED)
    row = db.execute(select(occupying, waiting).where(Registration.event_id == event_id)).one()
    return int(row[0]), int(row[1])
