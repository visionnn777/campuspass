"""Tickets and check-in (spec §11).

View    : read-only, no locks. Same token every time (viewing never rotates).
Reissue : event FOR SHARE -> own row FOR UPDATE -> ticket_version + 1.
Check-in: verify HMAC (no DB) -> event FOR SHARE -> row FOR UPDATE -> re-validate -> UPDATE.

FOR SHARE lets many door volunteers check people in at the same time, while an event
cancellation (FOR UPDATE) waits for them. A check-in that arrives after the cancellation
commits reads status=cancelled and is rejected. Invariant: checked_in_at <= cancelled_at.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import now_utc
from app.core.errors import AppError
from app.core.locks import (
    lock_event_for_share,
    lock_registration_by_id,
    lock_registration_of_user,
    lock_user_for_share,
    lock_users_for_share,
)
from app.core.tickets import build_ticket, parse_ticket
from app.models import Event, EventStatus, Registration, RegistrationStatus, User, UserRole
from app.schemas.registration import CheckinOut, TicketOut
from app.services.event_service import event_not_found, require_manage_access

CHECKIN_OPENS_BEFORE_START_HOURS = 2


def _ticket_not_available() -> AppError:
    return AppError(
        409,
        "TICKET_NOT_AVAILABLE",
        "A ticket is only available for a confirmed registration of a published event that has not ended.",
    )


def _invalid_ticket() -> AppError:
    # One generic answer for wrong token, wrong event, cancelled and obsolete tickets.
    return AppError(404, "INVALID_TICKET", "This ticket is not valid for this event.")


def _ticket_out(registration: Registration, event: Event, user: User, version: int) -> TicketOut:
    return TicketOut(
        token=build_ticket(registration.id, version),
        event_id=event.id,
        event_title=event.title,
        attendee_name=user.full_name,
        ticket_version=version,
    )


def view_ticket(db: Session, user: User, event_id: uuid.UUID) -> TicketOut:
    """No lock: nothing changes. If a cancellation commits a moment later, the token shown
    is already dead, because check-in re-validates status and version under its locks."""
    event = db.get(Event, event_id)
    if event is None or event.status == EventStatus.DRAFT:
        raise event_not_found()
    registration = db.scalars(
        select(Registration).where(Registration.event_id == event_id, Registration.user_id == user.id)
    ).one_or_none()
    if registration is None:
        raise AppError(404, "REGISTRATION_NOT_FOUND", "You have no registration for this event.")
    if user.role == UserRole.ORGANISER and event.organiser_id == user.id:
        raise AppError(403, "FORBIDDEN", "Organisers cannot attend their own events.")
    t = now_utc()
    if (
        registration.status != RegistrationStatus.CONFIRMED
        or event.status != EventStatus.PUBLISHED
        or t >= event.ends_at
    ):
        raise _ticket_not_available()
    return _ticket_out(registration, event, user, registration.ticket_version)


def reissue_ticket(db: Session, user: User, event_id: uuid.UUID) -> TicketOut:
    """'My ticket was copied': version + 1 makes every earlier token invalid."""
    with db.begin():
        # §8 Rule 2: SHARE, because reissue does not change the event, only one registration.
        event = lock_event_for_share(db, event_id)
        if event is None or event.status == EventStatus.DRAFT:
            raise event_not_found()
        registration = lock_registration_of_user(db, event.id, user.id)
        if registration is None:
            raise AppError(404, "REGISTRATION_NOT_FOUND", "You have no registration for this event.")
        # The ticket belongs to this user; serialize issuance with account deactivation too.
        current_user = lock_user_for_share(db, user.id)
        if current_user is None or not current_user.is_active:
            raise AppError(403, "ACCOUNT_DISABLED", "The account is disabled.")
        if current_user.role == UserRole.ORGANISER and event.organiser_id == current_user.id:
            raise AppError(403, "FORBIDDEN", "Organisers cannot attend their own events.")
        t = now_utc()  # clock is read only after event, registration, and user locks
        if (
            registration.status != RegistrationStatus.CONFIRMED
            or event.status != EventStatus.PUBLISHED
            or t >= event.ends_at
        ):
            raise _ticket_not_available()
        registration.ticket_version += 1
        db.flush()
        version = registration.ticket_version
    # Token is built AFTER commit (§8 Rule 5). Concurrent reissues are serialized by the
    # row lock; only the token from the LAST commit stays valid.
    return _ticket_out(registration, event, user, version)


def check_in(db: Session, actor: User, event_id: uuid.UUID, token: str) -> CheckinOut:
    # Step 0: verify the signature BEFORE touching the database (cheap rejection).
    parsed = parse_ticket(token)
    if parsed is None:
        raise _invalid_ticket()
    registration_id, token_version = parsed

    with db.begin():
        # Rule 1+2: event FOR SHARE -> registration FOR UPDATE -> user FOR SHARE.
        # The shared event lock permits parallel scanners but serializes against cancellation.
        event = require_manage_access(lock_event_for_share(db, event_id), actor)
        if event.status != EventStatus.PUBLISHED:
            raise AppError(409, "EVENT_NOT_ACTIVE", "Check-in is only possible for a published event.")

        # event_id in the WHERE clause binds the signed ticket to this exact event.
        registration = lock_registration_by_id(db, event.id, registration_id)
        if registration is None:
            raise _invalid_ticket()

        # Lock both the attendee and the scanner after event/registration locks, in UUID
        # order. This synchronizes eligibility with deactivation and ensures that a scanner
        # whose role/account was revoked while the request was in flight cannot act on a
        # stale `actor` object loaded by the authentication dependency.
        locked_users = lock_users_for_share(db, {registration.user_id, actor.id})
        attendee = locked_users.get(registration.user_id)
        current_actor = locked_users.get(actor.id)
        if attendee is None:
            raise _invalid_ticket()
        if current_actor is None or not current_actor.is_active:
            raise AppError(403, "ACCOUNT_DISABLED", "The scanner's account is disabled.")
        # Re-check role/ownership against the freshly locked actor after any lock wait.
        require_manage_access(event, current_actor)

        if registration.ticket_version != token_version:
            raise _invalid_ticket()
        if registration.status == RegistrationStatus.CHECKED_IN:
            used_at = registration.checked_in_at.isoformat() if registration.checked_in_at else "an earlier time"
            raise AppError(409, "ALREADY_CHECKED_IN", f"This ticket was already used at {used_at}.")
        if registration.status != RegistrationStatus.CONFIRMED:
            raise _invalid_ticket()
        if not attendee.is_active:
            raise AppError(409, "ATTENDEE_DISABLED", "The attendee's account is disabled.")
        if attendee.role == UserRole.ORGANISER and attendee.id == event.organiser_id:
            # An old registration must not become a valid admission if an account is
            # later promoted to organiser for this same event.
            raise _invalid_ticket()

        # Crucially, read the clock only AFTER all locks. A request that waited on a
        # registration or user row must not be accepted using a stale check-in timestamp.
        t = now_utc()
        opens = event.starts_at.timestamp() - CHECKIN_OPENS_BEFORE_START_HOURS * 3600
        if not (opens <= t.timestamp() <= event.ends_at.timestamp()):
            raise AppError(409, "CHECKIN_NOT_OPEN", "Check-in opens 2 hours before the start and closes at the end.")

        registration.status = RegistrationStatus.CHECKED_IN
        registration.checked_in_at = t
        registration.checked_in_by = actor.id
        db.flush()
        out = CheckinOut(
            registration_id=registration.id,
            attendee_name=attendee.full_name,  # volunteer compares with the college ID card
            checked_in_at=t,
        )
    return out
