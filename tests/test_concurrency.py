"""Spec §14 concurrency tests 1-8 and 14, against a LIVE server and real PostgreSQL.

* Threads are released together by a barrier, so requests truly collide.
* Every assertion checks the FINAL DATABASE STATE, not only HTTP status codes.
* Each test repeats for CONCURRENCY_ROUNDS rounds (default 3) to catch timing bugs.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.database import SessionLocal, engine
from app.main import app
from app.models import Event, Registration, RegistrationStatus, User, UserRole
from tests.conftest import API, Factory, TestClock, load_event, registration_of
from tests.live_server import LiveServer, run_concurrently

pytestmark = pytest.mark.concurrency
ROUNDS = int(os.environ.get("CONCURRENCY_ROUNDS", "3"))
rounds = pytest.mark.parametrize("round_no", range(ROUNDS))


@pytest.fixture(scope="module")
def server() -> Iterator[LiveServer]:
    live = LiveServer(app)
    live.start()
    yield live
    live.stop()


def bearer(factory: Factory, user: User) -> dict[str, str]:
    return factory.auth(user)


def statuses(event_id: object) -> Counter[str]:
    with SessionLocal() as db:
        rows = db.scalars(select(Registration.status).where(Registration.event_id == event_id)).all()
    return Counter(str(s) for s in rows)


def occupancy(event_id: object) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count()).where(
                    Registration.event_id == event_id,
                    Registration.status.in_([RegistrationStatus.CONFIRMED, RegistrationStatus.CHECKED_IN]),
                )
            )
            or 0
        )


def register_sequentially(server: LiveServer, factory: Factory, event: Event, users: list[User]) -> None:
    with server.client() as http:
        for user in users:
            assert http.put(f"{API}/events/{event.id}/registrations/me", headers=bearer(factory, user)).status_code == 201


# ---------------------------------------------------------------- test 1


@rounds
def test_1_fifty_students_ten_seats(server: LiveServer, factory: Factory, round_no: int) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=10, waitlist_limit=40)
    students = factory.users(50)
    headers = [bearer(factory, s) for s in students]

    def work(i: int, http: httpx.Client) -> httpx.Response:
        return http.put(f"{API}/events/{event.id}/registrations/me", headers=headers[i])

    responses = run_concurrently(server, 50, work)
    assert [r.status_code for r in responses] == [201] * 50
    assert statuses(event.id) == Counter({"confirmed": 10, "waitlisted": 40})
    positions = sorted(r.json()["queue_position"] for r in responses if r.json()["status"] == "waitlisted")
    assert positions == list(range(1, 41))  # FIFO positions with no gaps or duplicates
    with SessionLocal() as db:
        queue_nos = db.scalars(select(Registration.queue_no).where(Registration.event_id == event.id)).all()
    waiting_nos = [q for q in queue_nos if q is not None]
    assert len(waiting_nos) == len(set(waiting_nos)) == 40


# ---------------------------------------------------------------- test 2


@rounds
def test_2_fifty_requests_from_one_student(server: LiveServer, factory: Factory, round_no: int) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=10)
    student = factory.user()
    headers = bearer(factory, student)

    def work(i: int, http: httpx.Client) -> httpx.Response:
        return http.put(f"{API}/events/{event.id}/registrations/me", headers=headers)

    codes = Counter(r.status_code for r in run_concurrently(server, 50, work))
    assert codes == Counter({201: 1, 200: 49})
    assert statuses(event.id) == Counter({"confirmed": 1})


# ---------------------------------------------------------------- test 3


@rounds
def test_3_concurrent_cancellations_promote_fifo(server: LiveServer, factory: Factory, round_no: int) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=5, waitlist_limit=10)
    confirmed, waiting = factory.users(5), factory.users(10)
    register_sequentially(server, factory, event, confirmed + waiting)
    with SessionLocal() as db:
        queue = dict(
            db.execute(
                select(Registration.user_id, Registration.queue_no).where(
                    Registration.event_id == event.id, Registration.status == RegistrationStatus.WAITLISTED
                )
            ).all()
        )
    first_five = {uid for uid, _ in sorted(queue.items(), key=lambda kv: kv[1])[:5]}

    def work(i: int, http: httpx.Client) -> httpx.Response:
        return http.delete(f"{API}/events/{event.id}/registrations/me", headers=bearer(factory, confirmed[i]))

    assert all(r.status_code == 200 for r in run_concurrently(server, 5, work))
    with SessionLocal() as db:
        now_confirmed = set(
            db.scalars(
                select(Registration.user_id).where(
                    Registration.event_id == event.id, Registration.status == RegistrationStatus.CONFIRMED
                )
            ).all()
        )
    assert now_confirmed == first_five  # exactly the 5 lowest queue numbers
    assert occupancy(event.id) == 5
    assert statuses(event.id) == Counter({"confirmed": 5, "cancelled": 5, "waitlisted": 5})


# ---------------------------------------------------------------- tickets helpers


def confirmed_with_ticket(server: LiveServer, factory: Factory) -> tuple[User, Event, User, str]:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser)
    student = factory.user()
    register_sequentially(server, factory, event, [student])
    with server.client() as http:
        token = http.get(
            f"{API}/events/{event.id}/registrations/me/ticket", headers=bearer(factory, student)
        ).json()["token"]
    return organiser, event, student, token


def check_in_status(server: LiveServer, factory: Factory, event: Event, scanner: User, token: str) -> int:
    with server.client() as http:
        return http.post(
            f"{API}/events/{event.id}/checkins", json={"token": token}, headers=bearer(factory, scanner)
        ).status_code


# ---------------------------------------------------------------- test 4


@rounds
def test_4_twenty_scans_of_one_ticket(server: LiveServer, factory: Factory, clock: TestClock, round_no: int) -> None:
    organiser, event, student, token = confirmed_with_ticket(server, factory)
    clock.set(event.starts_at - timedelta(hours=1))
    headers = bearer(factory, organiser)

    def work(i: int, http: httpx.Client) -> httpx.Response:
        return http.post(f"{API}/events/{event.id}/checkins", json={"token": token}, headers=headers)

    responses = run_concurrently(server, 20, work)
    assert Counter(r.status_code for r in responses) == Counter({200: 1, 409: 19})
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {"ALREADY_CHECKED_IN"}
    reg = registration_of(event.id, student.id)
    assert reg is not None and reg.status == RegistrationStatus.CHECKED_IN


# ---------------------------------------------------------------- test 5


@pytest.mark.parametrize("round_no", range(max(ROUNDS, 5)))
def test_5_event_cancellation_vs_check_in(
    server: LiveServer, factory: Factory, clock: TestClock, round_no: int
) -> None:
    organiser, event, student, token = confirmed_with_ticket(server, factory)
    clock.tick_from(event.starts_at - timedelta(hours=1))  # a REAL ticking clock: order of times matters
    headers = bearer(factory, organiser)

    def work(i: int, http: httpx.Client) -> httpx.Response:
        if i == 0:
            return http.post(f"{API}/events/{event.id}/cancel", headers=headers)
        return http.post(f"{API}/events/{event.id}/checkins", json={"token": token}, headers=headers)

    cancel, checkin = run_concurrently(server, 2, work)
    assert cancel.status_code == 200
    final_event = load_event(event.id)
    reg = registration_of(event.id, student.id)
    assert reg is not None and final_event.cancelled_at is not None
    if checkin.status_code == 200:
        # Check-in won the lock: admitted while the event was still published.
        assert reg.status == RegistrationStatus.CHECKED_IN
        assert reg.checked_in_at is not None and reg.checked_in_at <= final_event.cancelled_at
    else:
        # Cancellation won: the waiting check-in then read "cancelled".
        assert checkin.status_code == 409 and checkin.json()["error"]["code"] == "EVENT_NOT_ACTIVE"
        assert reg.status == RegistrationStatus.CONFIRMED and reg.checked_in_at is None


# ---------------------------------------------------------------- test 6


@rounds
def test_6_ticket_view_and_reissue_vs_cancellation(
    server: LiveServer, factory: Factory, clock: TestClock, round_no: int
) -> None:
    organiser, event, student, first_token = confirmed_with_ticket(server, factory)
    headers = bearer(factory, student)
    ticket_url = f"{API}/events/{event.id}/registrations/me/ticket"

    def work(i: int, http: httpx.Client) -> httpx.Response:
        if i == 0:
            return http.delete(f"{API}/events/{event.id}/registrations/me", headers=headers)
        if i % 2:
            return http.get(ticket_url, headers=headers)
        return http.post(f"{ticket_url}/reissue", headers=headers)

    responses = run_concurrently(server, 7, work)
    assert responses[0].status_code == 200
    tokens = {first_token} | {r.json()["token"] for r in responses[1:] if r.status_code == 200}
    reg = registration_of(event.id, student.id)
    assert reg is not None and reg.status == RegistrationStatus.CANCELLED
    clock.set(event.starts_at - timedelta(hours=1))
    for token in tokens:  # every token ever handed out is now dead
        assert check_in_status(server, factory, event, organiser, token) == 404


# ---------------------------------------------------------------- test 7


@rounds
def test_7_concurrent_reissues(server: LiveServer, factory: Factory, clock: TestClock, round_no: int) -> None:
    organiser, event, student, first_token = confirmed_with_ticket(server, factory)
    headers = bearer(factory, student)

    def work(i: int, http: httpx.Client) -> httpx.Response:
        return http.post(f"{API}/events/{event.id}/registrations/me/ticket/reissue", headers=headers)

    responses = run_concurrently(server, 5, work)
    assert all(r.status_code == 200 for r in responses)
    versions = sorted(r.json()["ticket_version"] for r in responses)
    assert versions == [2, 3, 4, 5, 6]  # serialized by the row lock: each saw the previous commit
    reg = registration_of(event.id, student.id)
    assert reg is not None and reg.ticket_version == 6

    clock.set(event.starts_at - timedelta(hours=1))
    by_version = {r.json()["ticket_version"]: r.json()["token"] for r in responses}
    for version in (2, 3, 4, 5):
        assert check_in_status(server, factory, event, organiser, by_version[version]) == 404
    assert check_in_status(server, factory, event, organiser, first_token) == 404
    assert check_in_status(server, factory, event, organiser, by_version[6]) == 200


# ---------------------------------------------------------------- test 8


@rounds
def test_8_capacity_increase_vs_registrations(server: LiveServer, factory: Factory, round_no: int) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser, capacity=5, waitlist_limit=20)
    first_confirmed, first_waiting = factory.users(5), factory.users(5)
    register_sequentially(server, factory, event, first_confirmed + first_waiting)
    newcomers = factory.users(10)

    def work(i: int, http: httpx.Client) -> httpx.Response:
        if i == 0:
            return http.patch(f"{API}/events/{event.id}", json={"capacity": 8}, headers=bearer(factory, organiser))
        return http.put(f"{API}/events/{event.id}/registrations/me", headers=bearer(factory, newcomers[i - 1]))

    responses = run_concurrently(server, 11, work)
    assert responses[0].status_code == 200
    assert occupancy(event.id) == 8  # never above the new capacity, and freed seats are used
    with SessionLocal() as db:
        confirmed = set(
            db.scalars(
                select(Registration.user_id).where(
                    Registration.event_id == event.id, Registration.status == RegistrationStatus.CONFIRMED
                )
            ).all()
        )
    # Whatever the interleaving, the 3 new seats go to the 3 oldest waiters (FIFO).
    assert confirmed == {u.id for u in first_confirmed} | {u.id for u in first_waiting[:3]}


# ---------------------------------------------------------------- test 14


@rounds
def test_14_two_admins_disable_each_other(server: LiveServer, factory: Factory, round_no: int) -> None:
    admin_a, admin_b = factory.user(UserRole.ADMIN), factory.user(UserRole.ADMIN)
    pairs = [(admin_a, admin_b), (admin_b, admin_a)]

    def work(i: int, http: httpx.Client) -> httpx.Response:
        actor, target = pairs[i]
        return http.patch(f"{API}/admin/users/{target.id}", json={"is_active": False}, headers=bearer(factory, actor))

    responses = run_concurrently(server, 2, work)
    assert sorted(r.status_code for r in responses) != [200, 200]
    with SessionLocal() as db:
        active_admins = db.scalar(
            select(func.count()).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
    assert active_admins == 1


# ---------------------------------------------------------------- regression tests: user eligibility and stale check-in clocks


def _wait_for_blocked_query(*fragments: str, timeout: float = 1.5) -> None:
    """Wait until the live server's PostgreSQL query is visibly blocked on a row lock.

    This uses PostgreSQL's own activity view rather than a guessed sleep, making the
    race tests synchronize on a real database wait. These tests run only on PostgreSQL.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as observer:
            queries = observer.execute(
                text(
                    "SELECT query FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
                )
            ).scalars().all()
        for query in queries:
            if all(fragment.lower() in (query or "").lower() for fragment in fragments):
                return
        time.sleep(0.01)
    pytest.fail(f"No PostgreSQL query was observed waiting on a lock with fragments {fragments!r}")


def test_deactivation_committing_first_skips_waitlisted_user(
    server: LiveServer, factory: Factory
) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1, waitlist_limit=2)
    confirmed = factory.user()
    first_waitlisted, second_waitlisted = factory.users(2)
    register_sequentially(server, factory, event, [confirmed, first_waitlisted, second_waitlisted])
    assert registration_of(event.id, first_waitlisted.id).status == RegistrationStatus.WAITLISTED  # type: ignore[union-attr]

    with engine.connect() as holder:
        transaction = holder.begin()
        # This update is uncommitted and holds an exclusive lock on the waitlisted user's row.
        holder.execute(
            text("UPDATE users SET is_active = false WHERE id = :user_id"),
            {"user_id": first_waitlisted.id},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: _request_cancel(server, factory, event, confirmed)
            )
            _wait_for_blocked_query("from users", "for share")
            transaction.commit()  # deactivation commits before promotion can read eligibility
            response = future.result(timeout=10)

    assert response.status_code == 200
    first = registration_of(event.id, first_waitlisted.id)
    second = registration_of(event.id, second_waitlisted.id)
    assert first is not None and first.status == RegistrationStatus.WAITLISTED
    assert second is not None and second.status == RegistrationStatus.CONFIRMED
    assert occupancy(event.id) == 1


def _request_cancel(server: LiveServer, factory: Factory, event: Event, student: User) -> httpx.Response:
    with server.client() as client:
        return client.delete(
            f"{API}/events/{event.id}/registrations/me", headers=bearer(factory, student)
        )


def test_deactivation_committing_first_prevents_check_in(
    server: LiveServer, factory: Factory, clock: TestClock
) -> None:
    organiser, event, student, token = confirmed_with_ticket(server, factory)
    clock.set(event.starts_at - timedelta(hours=1))

    with engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(
            text("UPDATE users SET is_active = false WHERE id = :user_id"),
            {"user_id": student.id},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(check_in_status_response, server, factory, event, organiser, token)
            _wait_for_blocked_query("from users", "for share")
            transaction.commit()
            response = future.result(timeout=10)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ATTENDEE_DISABLED"
    registration = registration_of(event.id, student.id)
    assert registration is not None and registration.status == RegistrationStatus.CONFIRMED
    assert registration.checked_in_at is None


def test_scanner_deactivation_committing_first_prevents_check_in(
    server: LiveServer, factory: Factory, clock: TestClock
) -> None:
    organiser, event, student, token = confirmed_with_ticket(server, factory)
    clock.set(event.starts_at - timedelta(hours=1))

    with engine.connect() as holder:
        transaction = holder.begin()
        # The request authenticates while this update is uncommitted, then must wait on
        # the scanner's user row before acting. Once this commits, permission is rechecked.
        holder.execute(
            text("UPDATE users SET is_active = false WHERE id = :user_id"),
            {"user_id": organiser.id},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(check_in_status_response, server, factory, event, organiser, token)
            _wait_for_blocked_query("from users", "for share")
            transaction.commit()
            response = future.result(timeout=10)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"
    registration = registration_of(event.id, student.id)
    assert registration is not None and registration.status == RegistrationStatus.CONFIRMED
    assert registration.checked_in_at is None


def check_in_status_response(
    server: LiveServer, factory: Factory, event: Event, scanner: User, token: str
) -> httpx.Response:
    with server.client() as client:
        return client.post(
            f"{API}/events/{event.id}/checkins", json={"token": token}, headers=bearer(factory, scanner)
        )


def test_checkin_rechecks_clock_after_waiting_for_registration_lock(
    server: LiveServer, factory: Factory, clock: TestClock
) -> None:
    organiser, event, student, token = confirmed_with_ticket(server, factory)
    clock.set(event.starts_at - timedelta(hours=2) + timedelta(seconds=5))

    with engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(
            text("SELECT id FROM registrations WHERE event_id = :event_id AND user_id = :user_id FOR UPDATE"),
            {"event_id": event.id, "user_id": student.id},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(check_in_status_response, server, factory, event, organiser, token)
            _wait_for_blocked_query("from registrations", "for update")
            # Move the controllable business clock beyond the end while the request is blocked.
            clock.set(event.ends_at + timedelta(seconds=1))
            transaction.commit()
            response = future.result(timeout=10)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CHECKIN_NOT_OPEN"
    registration = registration_of(event.id, student.id)
    assert registration is not None and registration.status == RegistrationStatus.CONFIRMED
    assert registration.checked_in_at is None
