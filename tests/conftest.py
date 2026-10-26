"""Test setup (spec §14).

Order matters:
1. The DB guard runs at import time, BEFORE any app module is imported.
2. Environment is pointed at campuspass_test (APP_ENV=test, rate limits off by default).
3. `alembic upgrade head` builds the schema (proves migrations work; no create_all()).
4. Every test starts from empty tables, fresh rate-limit counters and a fixed clock.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from tests.db_guard import UnsafeTestDatabaseError, verify_connected_database, verify_test_database_url

ROOT = Path(__file__).resolve().parent.parent
_FILE_VALUES = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}


def _setting(name: str) -> str | None:
    return os.environ.get(name) or _FILE_VALUES.get(name)


TEST_DATABASE_URL = _setting("TEST_DATABASE_URL")
APP_DATABASE_URL = _setting("DATABASE_URL")

try:
    verify_test_database_url(TEST_DATABASE_URL, APP_DATABASE_URL)
except UnsafeTestDatabaseError as exc:
    pytest.exit(f"Refusing to run tests: {exc}", returncode=2)

assert TEST_DATABASE_URL is not None
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL  # the app under test only ever sees the test DB
os.environ["RATE_LIMITS_ENABLED"] = "false"  # re-enabled inside test_rate_limits.py
os.environ["DB_POOL_SIZE"] = "20"  # 50-thread concurrency tests need more connections
os.environ["DB_MAX_OVERFLOW"] = "40"
os.environ["DB_LOCK_TIMEOUT_MS"] = "2000"  # keeps the lock-timeout test fast
for secret_name in ("JWT_SECRET", "TICKET_SECRET"):  # allows CI runs without a .env file
    if not _setting(secret_name):
        os.environ[secret_name] = secrets.token_urlsafe(48)

# App imports only AFTER the environment is safe.
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.clock import reset_clock, set_clock  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.ratelimit import reset_rate_limits  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Event, EventCategory, EventStatus, Registration, User, UserRole  # noqa: E402

try:
    verify_connected_database(engine)
except UnsafeTestDatabaseError as exc:
    pytest.exit(f"Refusing to run tests: {exc}", returncode=2)

API = "/api/v1"
TEST_PASSWORD = "correct horse battery staple for tests"


# ------------------------------------------------------------------ schema & cleanup


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def _clean_state(_migrated_schema: None) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE registrations, events, users"))
    reset_rate_limits()
    yield


# ------------------------------------------------------------------ clock


class TestClock:
    """Frozen by default (exact window edges); can also tick in real time from a set point."""

    __test__ = False  # not a test class

    def __init__(self, start: datetime) -> None:
        self._fixed = start
        self._offset: timedelta | None = None

    def __call__(self) -> datetime:
        if self._offset is not None:
            return datetime.now(UTC) + self._offset
        return self._fixed

    @property
    def now(self) -> datetime:
        return self()

    def set(self, moment: datetime) -> None:
        self._fixed, self._offset = moment, None

    def advance(self, **delta: float) -> None:
        self.set(self() + timedelta(**delta))

    def tick_from(self, moment: datetime) -> None:
        """Real-time clock starting at `moment` (used where ordering of times matters)."""
        self._offset = moment - datetime.now(UTC)


@pytest.fixture(autouse=True)
def clock() -> Iterator[TestClock]:
    test_clock = TestClock(datetime.now(UTC).replace(microsecond=0))
    set_clock(test_clock)
    yield test_clock
    reset_clock()


# ------------------------------------------------------------------ factories


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(TEST_PASSWORD)  # hashed once: Argon2 is deliberately slow


class Factory:
    """Creates rows directly in the test database (fast set-up, no HTTP)."""

    __test__ = False

    def __init__(self, clock: TestClock, password_hash: str) -> None:
        self.clock = clock
        self.password_hash = password_hash
        self._n = 0

    def user(
        self,
        role: UserRole = UserRole.STUDENT,
        *,
        name: str | None = None,
        email: str | None = None,
        is_active: bool = True,
    ) -> User:
        self._n += 1
        with SessionLocal() as db:
            user = User(
                email=email or f"user{self._n}-{secrets.token_hex(3)}@example.com",
                full_name=name or f"Test {role.value.title()} {self._n}",
                password_hash=self.password_hash,
                role=role,
                is_active=is_active,
            )
            db.add(user)
            db.commit()
            return user

    def users(self, count: int, role: UserRole = UserRole.STUDENT) -> list[User]:
        return [self.user(role) for _ in range(count)]

    def token(self, user: User) -> str:
        return create_access_token(user.id)[0]

    def auth(self, user: User) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(user)}"}

    def event(
        self,
        organiser: User,
        *,
        status: EventStatus = EventStatus.PUBLISHED,
        capacity: int = 10,
        waitlist_limit: int = 50,
        title: str = "Intro to Databases",
        category: EventCategory = EventCategory.TECH,
        opens: timedelta = timedelta(hours=-1),
        closes: timedelta = timedelta(hours=1),
        starts: timedelta = timedelta(hours=3),
        ends: timedelta = timedelta(hours=5),
    ) -> Event:
        t = self.clock.now
        with SessionLocal() as db:
            event = Event(
                organiser_id=organiser.id,
                title=title,
                description="",
                category=category,
                venue="Seminar Hall",
                registration_opens_at=t + opens,
                registration_closes_at=t + closes,
                starts_at=t + starts,
                ends_at=t + ends,
                capacity=capacity,
                waitlist_limit=waitlist_limit,
                status=status,
                cancelled_at=t if status == EventStatus.CANCELLED else None,
            )
            db.add(event)
            db.commit()
            return event


@pytest.fixture
def factory(clock: TestClock, password_hash: str) -> Factory:
    return Factory(clock, password_hash)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


# ------------------------------------------------------------------ helpers


def registration_of(event_id: Any, user_id: Any) -> Registration | None:
    with SessionLocal() as session:
        return session.scalars(
            select(Registration).where(Registration.event_id == event_id, Registration.user_id == user_id)
        ).one_or_none()


def load_event(event_id: Any) -> Event:
    with SessionLocal() as session:
        event = session.get(Event, event_id)
        assert event is not None
        return event


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


RegistrationLookup = Callable[[Any, Any], Registration | None]
