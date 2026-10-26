"""Registration (PUT), cancellation, organiser removal and reads (spec §9, §10).

The core of the project. Every state change follows the lock protocol (§8):
    lock event FOR UPDATE -> lock registration -> read clock -> count -> decide -> commit
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.clock import now_utc
from app.core.errors import AppError
from app.core.locks import (
    count_occupancy,
    lock_event_for_update,
    lock_registration_by_id,
    lock_registration_of_user,
)
from app.models import (
    WAITLIST_QUEUE_SEQ,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    User,
    UserRole,
)
from app.models.enums import ACTIVE_REGISTRATION_STATUSES
from app.schemas.common import Page, PageParams
from app.schemas.registration import AttendeeOut, RegistrationOut
from app.services.event_service import event_not_found, require_manage_access
from app.services.promotion import promote


def _registration_not_found() -> AppError:
    return AppError(404, "REGISTRATION_NOT_FOUND", "You have no registration for this event.")


def queue_position(db: Session, registration: Registration) -> int | None:
    """1 + number of people ahead in this event's waitlist. Students never see queue_no."""
    if registration.status != RegistrationStatus.WAITLISTED or registration.queue_no is None:
        return None
    return db.scalar(
        select(func.count()).where(
            Registration.event_id == registration.event_id,
            Registration.status == RegistrationStatus.WAITLISTED,
            Registration.queue_no <= registration.queue_no,
        )
    )


def registration_out(db: Session, registration: Registration, event: Event) -> RegistrationOut:
    return RegistrationOut(
        id=registration.id,
        event_id=event.id,
        event_title=event.title,
        event_status=event.status,
        status=registration.status,
        queue_position=queue_position(db, registration),
        confirmed_at=registration.confirmed_at,
        waitlisted_at=registration.waitlisted_at,
        cancelled_at=registration.cancelled_at,
        checked_in_at=registration.checked_in_at,
    )


# ----------------------------------------------------------------- register (PUT)


def register(db: Session, user: User, event_id: uuid.UUID) -> tuple[RegistrationOut, bool]:
    """'Make sure I am registered.' Returns (registration, created).

    created=True  -> 201 (new row, or a cancelled row reactivated)
    created=False -> 200 (already confirmed / waitlisted / checked in: nothing changed)
    """
    with db.begin():
        # §8 Rule 1+2: exclusive event lock. Registrations for THIS event now take turns.
        event = lock_event_for_update(db, event_id)
        if event is None or event.status == EventStatus.DRAFT:
            raise event_not_found()
        t = now_utc()  # §8 Rule 3: the clock is read AFTER waiting for the lock

        if event.status == EventStatus.CANCELLED:
            raise AppError(409, "EVENT_CANCELLED", "This event has been cancelled.")
        if user.role == UserRole.ORGANISER and event.organiser_id == user.id:
            raise AppError(403, "ORGANISER_CANNOT_REGISTER", "Organisers cannot register for their own event.")

        registration = lock_registration_of_user(db, event.id, user.id)
        # Checked BEFORE the window: a retry after registration closes still gets its answer.
        if registration is not None and registration.status in ACTIVE_REGISTRATION_STATUSES:
            return registration_out(db, registration, event), False

        if t < event.registration_opens_at:
            raise AppError(409, "REGISTRATION_NOT_OPEN", "Registration has not opened yet.")
        if t >= event.registration_closes_at:
            raise AppError(409, "REGISTRATION_CLOSED", "Registration for this event has closed.")

        occupancy, waiting = count_occupancy(db, event.id)  # §8 Rule 3: count after the lock
        if occupancy < event.capacity:
            new_status = RegistrationStatus.CONFIRMED
        elif waiting < event.waitlist_limit:
            new_status = RegistrationStatus.WAITLISTED
        else:
            raise AppError(409, "EVENT_FULL", "This event and its waitlist are full.")

        if registration is None:
            registration = Registration(id=uuid.uuid4(), event_id=event.id, user_id=user.id)
            db.add(registration)
        else:
            _start_new_cycle(registration)  # re-registration reuses the row (spec §3)

        registration.status = new_status
        if new_status == RegistrationStatus.CONFIRMED:
            registration.confirmed_at = t
        else:
            # A new, larger number: re-registration never jumps the queue.
            registration.queue_no = db.execute(select(WAITLIST_QUEUE_SEQ.next_value())).scalar_one()
            registration.waitlisted_at = t
        db.flush()  # a duplicate row would fail HERE on uq_registrations_event_id (safety net)
        out = registration_out(db, registration, event)
    return out, True


def _start_new_cycle(registration: Registration) -> None:
    """Timestamps describe the CURRENT cycle only (spec §3). ticket_version is unchanged:
    the cancellation that ended the previous cycle already increased it."""
    registration.confirmed_at = None
    registration.waitlisted_at = None
    registration.cancelled_at = None
    registration.cancelled_by = None
    registration.queue_no = None


# ----------------------------------------------------------------- cancel / remove


def _cancel_locked(
    db: Session, event: Event, registration: Registration | None, actor: User, t: datetime
) -> Registration:
    """Shared by self-cancel and organiser removal. Caller holds event FOR UPDATE + row lock."""
    if registration is None:
        raise _registration_not_found()
    if registration.status == RegistrationStatus.CANCELLED:
        return registration  # repeat-safe: nothing to do
    if registration.status == RegistrationStatus.CHECKED_IN:
        raise AppError(409, "CANNOT_CANCEL_AFTER_CHECKIN", "Checked-in registrations cannot be cancelled.")
    if t >= event.starts_at:
        raise AppError(409, "EVENT_STARTED", "Registrations cannot be cancelled after the event starts.")

    was_confirmed = registration.status == RegistrationStatus.CONFIRMED
    registration.status = RegistrationStatus.CANCELLED
    registration.cancelled_at = t
    registration.cancelled_by = actor.id
    registration.queue_no = None
    if was_confirmed:
        # Kill every ticket issued so far: a later re-confirmation must not revive them.
        registration.ticket_version += 1
    db.flush()

    if was_confirmed:
        promote(db, event, 1, t)  # at most one seat was freed
    return registration


def cancel_own(db: Session, user: User, event_id: uuid.UUID) -> RegistrationOut:
    with db.begin():
        event = lock_event_for_update(db, event_id)  # §8 Rule 1: event first
        if event is None or event.status == EventStatus.DRAFT:
            raise event_not_found()
        registration = lock_registration_of_user(db, event.id, user.id)  # then the row
        t = now_utc()
        registration = _cancel_locked(db, event, registration, user, t)
        out = registration_out(db, registration, event)
    return out


def remove_registration(
    db: Session, actor: User, event_id: uuid.UUID, registration_id: uuid.UUID
) -> RegistrationOut:
    """Organiser/admin removes someone (duplicate account, in-person request, ...)."""
    with db.begin():
        event = require_manage_access(lock_event_for_update(db, event_id), actor)
        registration = lock_registration_by_id(db, event.id, registration_id)
        t = now_utc()
        if registration is None:
            raise AppError(404, "REGISTRATION_NOT_FOUND", "Registration not found for this event.")
        registration = _cancel_locked(db, event, registration, actor, t)
        out = registration_out(db, registration, event)
    return out


# ----------------------------------------------------------------- reads


def get_my_registration(db: Session, user: User, event_id: uuid.UUID) -> RegistrationOut:
    row = db.execute(
        select(Registration, Event)
        .join(Event, Event.id == Registration.event_id)
        .where(Registration.event_id == event_id, Registration.user_id == user.id)
    ).one_or_none()
    if row is None:
        raise _registration_not_found()
    registration, event = row
    return registration_out(db, registration, event)


def list_my_registrations(db: Session, user: User, page: PageParams) -> Page[RegistrationOut]:
    total = db.scalar(select(func.count()).where(Registration.user_id == user.id)) or 0
    rows = db.execute(
        select(Registration, Event)
        .join(Event, Event.id == Registration.event_id)
        .where(Registration.user_id == user.id)
        .order_by(Event.starts_at.desc(), Registration.id)
        .offset(page.offset)
        .limit(page.page_size)
    ).all()
    items = [registration_out(db, reg, ev) for reg, ev in rows]
    return Page[RegistrationOut](items=items, page=page.page, page_size=page.page_size, total=total)


def list_attendees(
    db: Session,
    actor: User,
    event_id: uuid.UUID,
    status: RegistrationStatus | None,
    page: PageParams,
) -> Page[AttendeeOut]:
    require_manage_access(db.get(Event, event_id), actor)

    # Waitlist position via a window function: queue_no ascending puts waitlisted rows
    # first (NULLs sort last), so row_number() equals the position in line.
    position = func.row_number().over(
        partition_by=Registration.event_id, order_by=Registration.queue_no.asc()
    )
    position_if_waiting = case(
        (Registration.status == RegistrationStatus.WAITLISTED, position), else_=None
    )
    filters = [Registration.event_id == event_id]
    if status is not None:
        filters.append(Registration.status == status)

    total = db.scalar(select(func.count()).select_from(Registration).where(*filters)) or 0
    rows = db.execute(
        select(Registration, User.full_name, User.email, position_if_waiting)
        .join(User, User.id == Registration.user_id)
        .where(*filters)
        .order_by(Registration.created_at, Registration.id)
        .offset(page.offset)
        .limit(page.page_size)
    ).all()
    items = [
        AttendeeOut(
            registration_id=reg.id,
            user_id=reg.user_id,
            full_name=name,
            email=email,
            status=reg.status,
            queue_position=pos,
            checked_in_at=reg.checked_in_at,
        )
        for reg, name, email, pos in rows
    ]
    return Page[AttendeeOut](items=items, page=page.page, page_size=page.page_size, total=total)
