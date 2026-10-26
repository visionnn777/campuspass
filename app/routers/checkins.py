"""/api/v1/events/{event_id}/checkins (spec §11). The token travels in the BODY."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.deps import DbSession, OrganiserOrAdmin
from app.core.ratelimit import user_limit
from app.schemas.registration import CheckinIn, CheckinOut
from app.services import ticket_service

router = APIRouter(prefix="/events/{event_id}/checkins", tags=["check-in"])


@router.post(
    "",
    response_model=CheckinOut,
    dependencies=[user_limit("checkin-scanner", "120/minute")],
    summary="Check an attendee in with their ticket (organiser or admin)",
)
def check_in(event_id: uuid.UUID, data: CheckinIn, actor: OrganiserOrAdmin, db: DbSession) -> CheckinOut:
    return ticket_service.check_in(db, actor, event_id, data.token)
