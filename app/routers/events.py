"""/api/v1/events: CRUD, publish, cancel, listing (spec §7)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.deps import DbSession, OptionalUser, OrganiserOrAdmin
from app.core.ratelimit import ip_limit
from app.schemas.common import Page, PageParams, page_params
from app.schemas.event import EventCreate, EventListQuery, EventOut, EventUpdate

from app.services import event_service

router = APIRouter(prefix="/events", tags=["events"])


@router.get(
    "",
    response_model=Page[EventOut],
    dependencies=[ip_limit("events-read", "120/minute")],
    summary="Browse published and cancelled events",
)
def list_events(
    db: DbSession,
    query: Annotated[EventListQuery, Query()],
    page: Annotated[PageParams, Depends(page_params)],
) -> Page[EventOut]:
    return event_service.list_public_events(db, query, page)


@router.get(
    "/{event_id}",
    response_model=EventOut,
    dependencies=[ip_limit("events-read", "120/minute")],
    summary="One event (drafts only for their owner or an admin)",
)
def get_event(event_id: uuid.UUID, db: DbSession, viewer: OptionalUser) -> EventOut:
    return event_service.get_event(db, viewer, event_id)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=EventOut, summary="Create a draft event")
def create_event(data: EventCreate, actor: OrganiserOrAdmin, db: DbSession) -> EventOut:
    return event_service.create_event(db, actor, data)


@router.patch("/{event_id}", response_model=EventOut, summary="Edit an event (owner or admin)")
def update_event(event_id: uuid.UUID, data: EventUpdate, actor: OrganiserOrAdmin, db: DbSession) -> EventOut:
    return event_service.update_event(db, actor, event_id, data)


@router.post("/{event_id}/publish", response_model=EventOut, summary="Publish a draft")
def publish_event(event_id: uuid.UUID, actor: OrganiserOrAdmin, db: DbSession) -> EventOut:
    return event_service.publish_event(db, actor, event_id)


@router.post("/{event_id}/cancel", response_model=EventOut, summary="Cancel a published event")
def cancel_event(event_id: uuid.UUID, actor: OrganiserOrAdmin, db: DbSession) -> EventOut:
    return event_service.cancel_event(db, actor, event_id)


@router.delete(
    "/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a draft",
)
def delete_event(event_id: uuid.UUID, actor: OrganiserOrAdmin, db: DbSession) -> Response:
    event_service.delete_event(db, actor, event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
