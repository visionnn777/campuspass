"""/api/v1/events/{event_id}/registrations/me/ticket: view and reissue (spec §11)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from app.core.deps import CurrentUser, DbSession
from app.core.ratelimit import user_limit
from app.core.tickets import render_qr_png
from app.schemas.registration import TicketOut
from app.services import ticket_service

router = APIRouter(prefix="/events/{event_id}/registrations/me/ticket", tags=["tickets"])

_PNG_RESPONSE = {200: {"content": {"image/png": {}}, "description": "TicketOut as JSON, or a QR PNG"}}


@router.get(
    "",
    response_model=TicketOut,
    responses=_PNG_RESPONSE,
    dependencies=[user_limit("ticket-view", "30/minute")],
    summary="View my ticket as JSON or as a QR PNG (same token; viewing never rotates it)",
)
def view_ticket(
    event_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    fmt: Annotated[Literal["json", "png"], Query(alias="format")] = "json",
) -> TicketOut | Response:
    ticket = ticket_service.view_ticket(db, user, event_id)
    if fmt == "png":
        return Response(content=render_qr_png(ticket.token), media_type="image/png")
    return ticket


@router.post(
    "/reissue",
    response_model=TicketOut,
    dependencies=[user_limit("ticket-reissue", "5/hour")],
    summary="Reissue my ticket (every earlier token stops working)",
)
def reissue_ticket(event_id: uuid.UUID, user: CurrentUser, db: DbSession) -> TicketOut:
    return ticket_service.reissue_ticket(db, user, event_id)
