"""/api/v1/me: the caller's own registrations and events."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, DbSession, OrganiserOrAdmin
from app.schemas.common import Page, PageParams, page_params
from app.schemas.event import EventOut
from app.schemas.registration import RegistrationOut
from app.services import event_service, registration_service

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/registrations", response_model=Page[RegistrationOut], summary="All my registrations")
def my_registrations(
    user: CurrentUser, db: DbSession, page: Annotated[PageParams, Depends(page_params)]
) -> Page[RegistrationOut]:
    return registration_service.list_my_registrations(db, user, page)


@router.get("/events", response_model=Page[EventOut], summary="Events I organise (all statuses)")
def my_events(
    actor: OrganiserOrAdmin, db: DbSession, page: Annotated[PageParams, Depends(page_params)]
) -> Page[EventOut]:
    return event_service.list_my_events(db, actor, page)
