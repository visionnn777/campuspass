"""Spec §14 test 10: the database itself rejects impossible rows (named constraints).

These bypass the application on purpose: even buggy code cannot store an invalid row.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import engine
from app.models import UserRole
from tests.conftest import Factory


def expect_violation(sql: str, params: dict[str, Any], constraint: str) -> None:
    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
    assert caught.value.orig.diag.constraint_name == constraint  # type: ignore[union-attr]


REG_SQL = (
    "INSERT INTO registrations (id, event_id, user_id, status, queue_no, confirmed_at, waitlisted_at, "
    "cancelled_at, cancelled_by, checked_in_at, checked_in_by) VALUES (:id, :event_id, :user_id, "
    "CAST(:status AS registration_status), :queue_no, :confirmed_at, :waitlisted_at, :cancelled_at, "
    ":cancelled_by, :checked_in_at, :checked_in_by)"
)


@pytest.fixture
def ids(factory: Factory) -> dict[str, uuid.UUID]:
    organiser = factory.user(UserRole.ORGANISER)
    student = factory.user()
    event = factory.event(organiser)
    return {"event_id": event.id, "user_id": student.id, "organiser_id": organiser.id}


def reg(ids: dict[str, uuid.UUID], **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "event_id": ids["event_id"],
        "user_id": ids["user_id"],
        "status": "confirmed",
        "queue_no": None,
        "confirmed_at": "2030-01-01T10:00:00Z",
        "waitlisted_at": None,
        "cancelled_at": None,
        "cancelled_by": None,
        "checked_in_at": None,
        "checked_in_by": None,
    }
    row.update(fields)
    return row


def test_non_waitlisted_row_cannot_keep_a_queue_number(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(REG_SQL, reg(ids, queue_no=7), "ck_registrations_waitlist_fields")


def test_waitlisted_row_needs_queue_number(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(
        REG_SQL, reg(ids, status="waitlisted", confirmed_at=None, waitlisted_at="2030-01-01T10:00:00Z"),
        "ck_registrations_waitlist_fields",
    )


def test_confirmed_row_needs_confirmed_at(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(REG_SQL, reg(ids, confirmed_at=None), "ck_registrations_confirmed_fields")


def test_checked_in_row_needs_who_and_when(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(
        REG_SQL, reg(ids, status="checked_in", checked_in_at="2030-01-01T11:00:00Z"),
        "ck_registrations_checkin_fields",
    )


def test_cancelled_row_needs_cancelled_at(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(REG_SQL, reg(ids, status="cancelled"), "ck_registrations_cancel_fields")


def test_cancelled_row_needs_actor(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(
        REG_SQL,
        reg(ids, status="cancelled", cancelled_at="2030-01-01T11:00:00Z", cancelled_by=None),
        "ck_registrations_cancel_fields",
    )


def test_one_row_per_student_per_event(ids: dict[str, uuid.UUID]) -> None:
    with engine.begin() as conn:
        conn.execute(text(REG_SQL), reg(ids))
    expect_violation(REG_SQL, reg(ids), "uq_registrations_event_id")


@pytest.mark.parametrize("email", [" padded@example.com", "Upper@Example.com"])
def test_email_must_be_normalized(email: str) -> None:
    expect_violation(
        "INSERT INTO users (id, email, password_hash, full_name) VALUES (:id, :email, 'x', 'X')",
        {"id": uuid.uuid4(), "email": email},
        "ck_users_email_normalized",
    )


EVENT_SQL = (
    "INSERT INTO events (id, organiser_id, title, category, venue, registration_opens_at, "
    "registration_closes_at, starts_at, ends_at, capacity, status, cancelled_at) VALUES (:id, :org, 'T', "
    "'tech', 'V', :o, :c, :s, :e, :cap, CAST(:status AS event_status), :cancelled_at)"
)


def event_params(ids: dict[str, uuid.UUID], **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org": ids["organiser_id"],
        "o": "2030-01-01T08:00:00Z",
        "c": "2030-01-01T09:00:00Z",
        "s": "2030-01-01T10:00:00Z",
        "e": "2030-01-01T12:00:00Z",
        "cap": 10,
        "status": "published",
        "cancelled_at": None,
    }
    row.update(fields)
    return row


def test_event_time_order(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(EVENT_SQL, event_params(ids, e="2030-01-01T09:30:00Z"), "ck_events_time_order")


def test_event_capacity_range(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(EVENT_SQL, event_params(ids, cap=0), "ck_events_capacity")


def test_cancelled_event_needs_cancelled_at(ids: dict[str, uuid.UUID]) -> None:
    expect_violation(EVENT_SQL, event_params(ids, status="cancelled"), "ck_events_cancelled_at")
