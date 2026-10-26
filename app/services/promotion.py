"""FIFO waitlist promotion: the ONE query path used by cancel/removal/capacity increase.

The caller MUST hold the event's FOR UPDATE lock. Registration rows are locked in FIFO
order first; all associated user rows are then locked in UUID order. This lets account
 deactivation serialize against promotion without creating an inconsistent lock order.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, EventStatus, Registration, RegistrationStatus, User, UserRole


def promote(db: Session, event: Event, n: int, t: datetime) -> int:
    """Promote up to `n` eligible waitlisted users in FIFO order. Returns count promoted.

    The list is bounded by event.waitlist_limit (max 10,000). Locking the waiting rows
    first prevents a cancellation/re-registration from changing queue membership midway;
    locking user rows in sorted UUID order then serializes eligibility against deactivation.
    """
    if n <= 0 or event.status != EventStatus.PUBLISHED or t >= event.starts_at:
        return 0

    waiting_rows = db.scalars(
        select(Registration)
        .where(
            Registration.event_id == event.id,
            Registration.status == RegistrationStatus.WAITLISTED,
        )
        .order_by(Registration.queue_no, Registration.id)
        .with_for_update(of=Registration)
        .execution_options(populate_existing=True)
    ).all()
    if not waiting_rows:
        return 0

    # Deterministic lock order across concurrent promotions for different events.
    user_ids = sorted({row.user_id for row in waiting_rows}, key=str)
    locked_users = db.scalars(
        select(User)
        .where(User.id.in_(user_ids))
        .order_by(User.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).all()
    active_user_ids = {
        user.id
        for user in locked_users
        if user.is_active
        and not (user.role == UserRole.ORGANISER and user.id == event.organiser_id)
    }

    promoted_count = 0
    for registration in waiting_rows:  # already ordered FIFO
        if promoted_count >= n:
            break
        if registration.user_id not in active_user_ids:
            # Inactive users and organisers who own this event stay waitlisted and are skipped.
            continue
        registration.status = RegistrationStatus.CONFIRMED
        registration.confirmed_at = t
        registration.queue_no = None
        promoted_count += 1

    db.flush()
    return promoted_count
