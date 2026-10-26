"""Spec §14 test 13: database errors are mapped precisely (never a blanket mapping)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, TimeoutError as PoolTimeoutError

from app.core.database import engine
from app.core.errors import register_exception_handlers
from app.core.db_errors import map_db_error
from app.models import UserRole
from tests.conftest import API, Factory, error_code


@dataclass
class FakeDiag:
    constraint_name: str | None


class FakeDriverError(Exception):
    def __init__(self, sqlstate: str | None, constraint: str | None = None) -> None:
        super().__init__("fake")
        self.sqlstate = sqlstate
        self.diag = FakeDiag(constraint)


@pytest.mark.parametrize(
    "exc_type,sqlstate,constraint,status,code",
    [
        (IntegrityError, "23505", "uq_users_email", 409, "EMAIL_TAKEN"),
        (IntegrityError, "23505", "uq_registrations_event_id", 409, "ALREADY_REGISTERED"),
        (IntegrityError, "23514", "ck_events_time_order", 422, "INVALID_EVENT_TIMES"),
        # Unknown constraint => a bug => 500, NEVER a business conflict.
        (IntegrityError, "23505", "uq_something_else", 500, "INTERNAL_ERROR"),
        (IntegrityError, "23514", "ck_registrations_waitlist_fields", 500, "INTERNAL_ERROR"),
        (OperationalError, "55P03", None, 503, "BUSY"),
        (OperationalError, "57014", None, 503, "BUSY"),
        (OperationalError, "40P01", None, 503, "BUSY"),
        (OperationalError, None, None, 503, "DATABASE_UNAVAILABLE"),
        (OperationalError, "08006", None, 503, "DATABASE_UNAVAILABLE"),
        (ProgrammingError, "42703", None, 500, "INTERNAL_ERROR"),
    ],
)
def test_mapping_table(
    exc_type: type[IntegrityError], sqlstate: str | None, constraint: str | None, status: int, code: str
) -> None:
    error, level = map_db_error(exc_type("SELECT 1", {}, FakeDriverError(sqlstate, constraint)))
    assert (error.status_code, error.code) == (status, code)
    if code in ("INTERNAL_ERROR", "ALREADY_REGISTERED") or sqlstate == "40P01":
        assert level == logging.ERROR
    if code == "BUSY":
        assert error.headers["Retry-After"] == "1"


def test_real_duplicate_signup_maps_to_email_taken(client: TestClient) -> None:
    body = {"email": "dup@example.com", "full_name": "Dup User", "password": "a long and unusual phrase"}
    assert client.post(f"{API}/auth/register", json=body).status_code == 201
    response = client.post(f"{API}/auth/register", json=body)
    assert response.status_code == 409 and error_code(response) == "EMAIL_TAKEN"


def test_lock_timeout_returns_503_busy(client: TestClient, factory: Factory) -> None:
    """Another transaction holds the event lock; ours gives up after lock_timeout (2 s in tests)."""
    event = factory.event(factory.user(UserRole.ORGANISER))
    student = factory.user()
    with engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(text("SELECT id FROM events WHERE id = :id FOR UPDATE"), {"id": event.id})
        try:
            response = client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(student))
        finally:
            transaction.rollback()
    assert response.status_code == 503
    assert error_code(response) == "BUSY" and response.headers["Retry-After"] == "1"
    # Nothing was written by the timed-out request.
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM registrations")).scalar_one()
    assert count == 0



def test_connection_pool_timeout_maps_to_busy() -> None:
    error, level = map_db_error(PoolTimeoutError("connection pool exhausted"))
    assert error.status_code == 503
    assert error.code == "BUSY"
    assert error.headers["Retry-After"] == "1"
    assert level == logging.WARNING


def test_connection_pool_timeout_uses_standard_http_error_handler() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mini = FastAPI()
    register_exception_handlers(mini)

    @mini.get("/pool-timeout")
    def pool_timeout() -> None:
        raise PoolTimeoutError("private pool details must not escape")

    response = TestClient(mini, raise_server_exceptions=False).get("/pool-timeout")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BUSY"
    assert "private pool details" not in response.text
    assert response.headers["Retry-After"] == "1"
