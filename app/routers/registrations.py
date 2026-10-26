"""/api/v1/events/{event_id}/registrations (spec §9, §10)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.deps import CurrentUser, DbSession, OrganiserOrAdmin
from app.core.ratelimit import ip_limit, user_limit
from app.models import RegistrationStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.registration import AttendeeOut, RegistrationOut
from app.services import registration_service

router = APIRouter(prefix="/events/{event_id}/registrations", tags=["registrations"])


@router.put(
    "/me",
    response_model=RegistrationOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RegistrationOut, "description": "Already registered: nothing changed"}},
    dependencies=[user_limit("register-account", "10/minute"), ip_limit("register-ip", "60/minute")],
    summary="Make sure I am registered (201 created / 200 already registered)",
)
def register(event_id: uuid.UUID, user: CurrentUser, db: DbSession, response: Response) -> RegistrationOut:
    registration, created = registration_service.register(db, user, event_id)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return registration


@router.get("/me", response_model=RegistrationOut, summary="My registration (with waitlist position)")
def my_registration(event_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RegistrationOut:
    return registration_service.get_my_registration(db, user, event_id)


@router.delete(
    "/me",
    response_model=RegistrationOut,
    dependencies=[user_limit("cancel-account", "10/minute")],
    summary="Cancel my registration (promotes the next person in line)",
)
def cancel_my_registration(event_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RegistrationOut:
    return registration_service.cancel_own(db, user, event_id)


@router.get("", response_model=Page[AttendeeOut], summary="Attendee list (organiser or admin)")
def list_attendees(
    event_id: uuid.UUID,
    actor: OrganiserOrAdmin,
    db: DbSession,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[RegistrationStatus | None, Query(alias="status")] = None,
) -> Page[AttendeeOut]:
    return registration_service.list_attendees(db, actor, event_id, status_filter, page)


@router.delete(
    "/{registration_id}",
    response_model=RegistrationOut,
    summary="Remove someone's registration (organiser or admin)",
)
def remove_registration(
    event_id: uuid.UUID, registration_id: uuid.UUID, actor: OrganiserOrAdmin, db: DbSession
) -> RegistrationOut:
    return registration_service.remove_registration(db, actor, event_id, registration_id)
