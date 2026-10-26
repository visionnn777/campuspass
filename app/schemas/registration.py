"""Registration, ticket and check-in schemas (spec §19)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import EventStatus, RegistrationStatus


class RegistrationOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    event_title: str
    event_status: EventStatus
    status: RegistrationStatus
    # "You are 3rd in line". Only set while waitlisted. (queue_no itself is internal.)
    queue_position: int | None
    confirmed_at: datetime | None
    waitlisted_at: datetime | None
    cancelled_at: datetime | None
    checked_in_at: datetime | None


class AttendeeOut(BaseModel):
    registration_id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    email: str
    status: RegistrationStatus
    queue_position: int | None
    checked_in_at: datetime | None


class TicketOut(BaseModel):
    token: str
    event_id: uuid.UUID
    event_title: str
    attendee_name: str
    ticket_version: int


class CheckinIn(BaseModel):
    # The token travels in the BODY, never the URL (URLs end up in logs and history).
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=128)


class CheckinOut(BaseModel):
    registration_id: uuid.UUID
    attendee_name: str
    checked_in_at: datetime
