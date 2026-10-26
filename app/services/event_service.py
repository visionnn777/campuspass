"""Events: create, read, list, edit, publish, cancel, delete (spec §7).

Every state change takes the event's FOR UPDATE lock first (§8 Rule 1/2).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.clock import now_utc
from app.core.errors import AppError
from app.core.locks import count_occupancy, lock_event_for_update
from app.models import Event, EventStatus, Registration, RegistrationStatus, User, UserRole
from app.models.enums import OCCUPYING_STATUSES
from app.schemas.common import Page, PageParams
from app.schemas.event import EventCreate, EventListQuery, EventOut, EventUpdate, times_in_order
from app.services.promotion import promote

_SORTS = {
    "starts_at": (Event.starts_at.asc(), Event.id.asc()),
    "-starts_at": (Event.starts_at.desc(), Event.id.asc()),
    "created_at": (Event.created_at.asc(), Event.id.asc()),
    "-created_at": (Event.created_at.desc(), Event.id.asc()),
}

_TIME_FIELDS = ("registration_opens_at", "registration_closes_at", "starts_at", "ends_at")


def event_not_found() -> AppError:
    return AppError(404, "EVENT_NOT_FOUND", "Event not found.")


def can_manage(event: Event, actor: User) -> bool:
    return actor.role == UserRole.ADMIN or event.organiser_id == actor.id


def require_manage_access(event: Event | None, actor: User) -> Event:
    """Owner or admin. Someone else's DRAFT is a 404 (a 403 would confirm it exists)."""
    if event is None:
        raise event_not_found()
    if can_manage(event, actor):
        return event
    if event.status == EventStatus.DRAFT:
        raise event_not_found()
    raise AppError(403, "FORBIDDEN", "Only the event's organiser or an admin can do this.")


def build_event_out(event: Event, organiser_name: str, occupancy: int, waiting: int, t: datetime) -> EventOut:
    return EventOut(
        id=event.id,
        organiser_id=event.organiser_id,
        organiser_name=organiser_name,
        title=event.title,
        description=event.description,
        category=event.category,
        venue=event.venue,
        registration_opens_at=event.registration_opens_at,
        registration_closes_at=event.registration_closes_at,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        capacity=event.capacity,
        waitlist_limit=event.waitlist_limit,
        status=event.status,
        cancelled_at=event.cancelled_at,
        created_at=event.created_at,
        updated_at=event.updated_at,
        is_completed=t >= event.ends_at,
        is_registration_open=(
            event.status == EventStatus.PUBLISHED
            and event.registration_opens_at <= t < event.registration_closes_at
        ),
        seats_left=max(0, event.capacity - occupancy),
        waitlist_count=waiting,
    )


def _event_out_now(db: Session, event: Event) -> EventOut:
    occupancy, waiting = count_occupancy(db, event.id)
    organiser_name = db.scalar(select(User.full_name).where(User.id == event.organiser_id)) or ""
    return build_event_out(event, organiser_name, occupancy, waiting, now_utc())


# --------------------------------------------------------------------- reads


def _list_statement(filters: list) -> Select:
    """One statement: events + organiser name + counts as correlated subqueries.

    Counts are computed only for the rows on the requested page (no N+1 queries).
    """
    occupancy = (
        select(func.count())
        .where(Registration.event_id == Event.id, Registration.status.in_(OCCUPYING_STATUSES))
        .correlate(Event)
        .scalar_subquery()
    )
    waiting = (
        select(func.count())
        .where(Registration.event_id == Event.id, Registration.status == RegistrationStatus.WAITLISTED)
        .correlate(Event)
        .scalar_subquery()
    )
    return (
        select(Event, User.full_name, occupancy, waiting)
        .join(User, User.id == Event.organiser_id)
        .where(*filters)
    )


def _escape_like(text: str) -> str:
    """User input is literal: '%' and '_' must not act as wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _page_of_events(db: Session, filters: list, order_by: tuple, page: PageParams) -> Page[EventOut]:
    t = now_utc()
    total = db.scalar(select(func.count()).select_from(Event).where(*filters)) or 0
    rows = db.execute(
        _list_statement(filters).order_by(*order_by).offset(page.offset).limit(page.page_size)
    ).all()
    items = [build_event_out(ev, name, occ, wait, t) for ev, name, occ, wait in rows]
    return Page[EventOut](items=items, page=page.page, page_size=page.page_size, total=total)


def list_public_events(db: Session, query: EventListQuery, page: PageParams) -> Page[EventOut]:
    filters: list = [Event.status.in_([EventStatus.PUBLISHED, EventStatus.CANCELLED])]
    if query.search:
        filters.append(Event.title.ilike(f"%{_escape_like(query.search)}%", escape="\\"))
    if query.category is not None:
        filters.append(Event.category == query.category)
    if query.starts_after is not None:
        filters.append(Event.starts_at >= query.starts_after)
    if query.starts_before is not None:
        filters.append(Event.starts_at <= query.starts_before)
    if not query.include_past:
        filters.append(Event.ends_at > now_utc())
    return _page_of_events(db, filters, _SORTS[query.sort], page)


def list_my_events(db: Session, actor: User, page: PageParams) -> Page[EventOut]:
    return _page_of_events(db, [Event.organiser_id == actor.id], _SORTS["-created_at"], page)


def get_event(db: Session, viewer: User | None, event_id: uuid.UUID) -> EventOut:
    event = db.get(Event, event_id)
    if event is None:
        raise event_not_found()
    if event.status == EventStatus.DRAFT and (viewer is None or not can_manage(event, viewer)):
        raise event_not_found()
    return _event_out_now(db, event)


# --------------------------------------------------------------------- writes


def create_event(db: Session, actor: User, data: EventCreate) -> EventOut:
    t = now_utc()
    if data.starts_at <= t:
        raise AppError(422, "INVALID_EVENT_TIMES", "An event must start in the future.")
    with db.begin():
        event = Event(organiser_id=actor.id, status=EventStatus.DRAFT, **data.model_dump())
        db.add(event)
        db.flush()
        out = build_event_out(event, actor.full_name, 0, 0, t)
    return out


def update_event(db: Session, actor: User, event_id: uuid.UUID, data: EventUpdate) -> EventOut:
    changes = data.model_dump(exclude_unset=True)
    with db.begin():
        event = require_manage_access(lock_event_for_update(db, event_id), actor)  # §8 Rule 1/2
        t = now_utc()  # §8 Rule 3: read the clock after the lock

        if event.status == EventStatus.CANCELLED:
            raise AppError(409, "INVALID_STATE", "Cancelled events cannot be edited.")
        if t >= event.starts_at:
            raise AppError(409, "EVENT_STARTED", "Events cannot be edited after they start.")

        new_times = {f: changes.get(f, getattr(event, f)) for f in _TIME_FIELDS}
        if not times_in_order(*(new_times[f] for f in _TIME_FIELDS)):
            raise AppError(
                422,
                "INVALID_EVENT_TIMES",
                "Times must satisfy registration_opens_at < registration_closes_at <= starts_at < ends_at.",
            )
        if event.status == EventStatus.PUBLISHED and new_times["starts_at"] <= t:
            raise AppError(422, "INVALID_EVENT_TIMES", "A published event must start in the future.")

        occupancy, _ = count_occupancy(db, event.id)  # after the lock (Rule 3)
        if "capacity" in changes and changes["capacity"] < occupancy:
            raise AppError(
                409,
                "CAPACITY_BELOW_OCCUPANCY",
                f"Capacity cannot be lower than the {occupancy} people already holding seats.",
            )

        for field, value in changes.items():
            setattr(event, field, value)
        db.flush()

        if "capacity" in changes:
            # Freed seats never sit empty while people wait: promote in the SAME transaction.
            promote(db, event, event.capacity - occupancy, t)
        out = _event_out_now(db, event)
    return out


def publish_event(db: Session, actor: User, event_id: uuid.UUID) -> EventOut:
    with db.begin():
        event = require_manage_access(lock_event_for_update(db, event_id), actor)
        t = now_utc()
        if event.status != EventStatus.DRAFT:
            raise AppError(409, "INVALID_STATE", "Only draft events can be published.")
        if t >= event.registration_closes_at:
            raise AppError(409, "REGISTRATION_CLOSED", "Registration for this event has already closed.")
        event.status = EventStatus.PUBLISHED
        db.flush()
        out = _event_out_now(db, event)
    return out


def cancel_event(db: Session, actor: User, event_id: uuid.UUID) -> EventOut:
    """Registrations are kept unchanged (history). Registration, promotion, reissue and
    check-in all stop, because each of them re-checks the status under its own lock."""
    with db.begin():
        # FOR UPDATE: waits for any in-flight check-ins (they hold FOR SHARE) to finish.
        event = require_manage_access(lock_event_for_update(db, event_id), actor)
        t = now_utc()
        if event.status == EventStatus.DRAFT:
            raise AppError(409, "INVALID_STATE", "Drafts cannot be cancelled; delete them instead.")
        if event.status == EventStatus.CANCELLED:
            raise AppError(409, "INVALID_STATE", "This event is already cancelled.")
        if t >= event.ends_at:
            raise AppError(409, "EVENT_ENDED", "This event has already ended.")
        event.status = EventStatus.CANCELLED
        event.cancelled_at = t
        db.flush()
        out = _event_out_now(db, event)
    return out


def delete_event(db: Session, actor: User, event_id: uuid.UUID) -> None:
    with db.begin():
        event = require_manage_access(lock_event_for_update(db, event_id), actor)
        if event.status != EventStatus.DRAFT:
            raise AppError(409, "INVALID_STATE", "Only drafts can be deleted; cancel published events.")
        db.delete(event)
