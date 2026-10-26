"""Event schemas (spec §7, §19)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import EventCategory, EventStatus

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=150)]
Venue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Capacity = Annotated[int, Field(ge=1, le=10000)]
WaitlistLimit = Annotated[int, Field(ge=0, le=10000)]

EventSort = Literal["starts_at", "-starts_at", "created_at", "-created_at"]


def times_in_order(opens: datetime, closes: datetime, starts: datetime, ends: datetime) -> bool:
    """registration_opens_at < registration_closes_at <= starts_at < ends_at."""
    return opens < closes <= starts < ends


class EventCreate(BaseModel):
    # AwareDatetime: "2026-11-05T10:00:00" (no offset) is rejected with 422; the server
    # never guesses whether you meant IST or UTC.
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: Description = ""
    category: EventCategory
    venue: Venue
    registration_opens_at: AwareDatetime
    registration_closes_at: AwareDatetime
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    capacity: Capacity
    waitlist_limit: WaitlistLimit = 50

    @model_validator(mode="after")
    def _time_order(self) -> EventCreate:
        if not times_in_order(
            self.registration_opens_at, self.registration_closes_at, self.starts_at, self.ends_at
        ):
            raise ValueError(
                "times must satisfy registration_opens_at < registration_closes_at "
                "<= starts_at < ends_at"
            )
        return self


class EventUpdate(BaseModel):
    """Any subset of fields. `status` and `organiser_id` are not accepted (extra=forbid)."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    description: Description | None = None
    category: EventCategory | None = None
    venue: Venue | None = None
    registration_opens_at: AwareDatetime | None = None
    registration_closes_at: AwareDatetime | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    capacity: Capacity | None = None
    waitlist_limit: WaitlistLimit | None = None

    @model_validator(mode="after")
    def _no_explicit_nulls(self) -> EventUpdate:
        # {"title": null} would otherwise reach a NOT NULL column and become a 500.
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if not self.model_fields_set:
            raise ValueError("provide at least one field to change")
        return self


class EventOut(BaseModel):
    id: uuid.UUID
    organiser_id: uuid.UUID
    organiser_name: str
    title: str
    description: str
    category: EventCategory
    venue: str
    registration_opens_at: datetime
    registration_closes_at: datetime
    starts_at: datetime
    ends_at: datetime
    capacity: int
    waitlist_limit: int
    status: EventStatus
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Display snapshots. The authoritative decision is always made under the lock (§9).
    is_completed: bool
    is_registration_open: bool
    seats_left: int
    waitlist_count: int


class EventListQuery(BaseModel):
    search: str | None = Field(default=None, max_length=100)
    category: EventCategory | None = None
    starts_after: AwareDatetime | None = None
    starts_before: AwareDatetime | None = None
    include_past: bool = False
    sort: EventSort = "starts_at"
