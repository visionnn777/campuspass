"""Step 6: registration (PUT), cancellation, waitlist, promotion (spec §9, §10)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.core.database import SessionLocal
from app.models import Event, EventStatus, RegistrationStatus, User, UserRole
from tests.conftest import API, Factory, TestClock, error_code, registration_of


def put(client: TestClient, factory: Factory, event: Event, user: User):  # type: ignore[no-untyped-def]
    return client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(user))


def delete(client: TestClient, factory: Factory, event: Event, user: User):  # type: ignore[no-untyped-def]
    return client.delete(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(user))


def test_put_is_repeat_safe_201_then_200(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER))
    student = factory.user()
    first = put(client, factory, event, student)
    second = put(client, factory, event, student)
    assert first.status_code == 201 and first.json()["status"] == "confirmed"
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]


def test_waitlist_and_full(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1, waitlist_limit=1)
    a, b, c = factory.users(3)
    assert put(client, factory, event, a).json()["status"] == "confirmed"
    waiting = put(client, factory, event, b).json()
    assert waiting["status"] == "waitlisted" and waiting["queue_position"] == 1
    full = put(client, factory, event, c)
    assert full.status_code == 409 and error_code(full) == "EVENT_FULL"


def test_registration_window_edges(client: TestClient, factory: Factory, clock: TestClock) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER))
    early, late, registered = factory.users(3)

    clock.set(event.registration_opens_at - timedelta(microseconds=1))
    response = put(client, factory, event, early)
    assert response.status_code == 409 and error_code(response) == "REGISTRATION_NOT_OPEN"

    clock.set(event.registration_opens_at)
    assert put(client, factory, event, registered).status_code == 201

    clock.set(event.registration_closes_at)  # closes_at itself is already closed
    response = put(client, factory, event, late)
    assert response.status_code == 409 and error_code(response) == "REGISTRATION_CLOSED"
    # A retry from someone already registered still gets its answer after closing.
    assert put(client, factory, event, registered).status_code == 200


def test_event_state_rules(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    student = factory.user()
    draft = factory.event(organiser, status=EventStatus.DRAFT)
    cancelled = factory.event(organiser, status=EventStatus.CANCELLED)
    own = factory.event(organiser)
    assert error_code(put(client, factory, draft, student)) == "EVENT_NOT_FOUND"
    assert error_code(put(client, factory, cancelled, student)) == "EVENT_CANCELLED"
    response = put(client, factory, own, organiser)
    assert response.status_code == 403 and error_code(response) == "ORGANISER_CANNOT_REGISTER"
    assert client.put(f"{API}/events/{own.id}/registrations/me").status_code == 401


def test_admin_can_register_for_an_event_they_created(client: TestClient, factory: Factory) -> None:
    admin = factory.user(UserRole.ADMIN)
    event = factory.event(admin, capacity=1)
    response = put(client, factory, event, admin)
    assert response.status_code == 201
    assert response.json()["status"] == "confirmed"


def test_cancel_promotes_first_in_line(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1, waitlist_limit=5)
    a, b, c = factory.users(3)
    for s in (a, b, c):
        put(client, factory, event, s)
    response = delete(client, factory, event, a)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert registration_of(event.id, b.id).status == RegistrationStatus.CONFIRMED  # type: ignore[union-attr]
    c_now = client.get(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(c)).json()
    assert c_now["status"] == "waitlisted" and c_now["queue_position"] == 1


def test_cancelling_a_waitlist_entry_promotes_nobody(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1)
    a, b, c = factory.users(3)
    for s in (a, b, c):
        put(client, factory, event, s)
    delete(client, factory, event, b)
    assert registration_of(event.id, a.id).status == RegistrationStatus.CONFIRMED  # type: ignore[union-attr]
    c_reg = registration_of(event.id, c.id)
    assert c_reg is not None and c_reg.status == RegistrationStatus.WAITLISTED
    assert client.get(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(c)).json()["queue_position"] == 1


def test_cancel_edge_cases(client: TestClient, factory: Factory, clock: TestClock) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER))
    student, stranger = factory.users(2)
    put(client, factory, event, student)
    assert delete(client, factory, event, student).status_code == 200
    again = delete(client, factory, event, student)  # repeat-safe
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    missing = delete(client, factory, event, stranger)
    assert missing.status_code == 404 and error_code(missing) == "REGISTRATION_NOT_FOUND"

    put(client, factory, event, student)  # re-register, then the event starts
    clock.set(event.starts_at)
    started = delete(client, factory, event, student)
    assert started.status_code == 409 and error_code(started) == "EVENT_STARTED"


def test_reregistration_goes_to_the_back_of_the_queue(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1, waitlist_limit=5)
    a, b, c = factory.users(3)
    for s in (a, b, c):
        put(client, factory, event, s)
    delete(client, factory, event, b)  # B leaves the waitlist (position 1)
    back = put(client, factory, event, b)
    assert back.status_code == 201
    assert back.json()["status"] == "waitlisted" and back.json()["queue_position"] == 2  # behind C
    reg = registration_of(event.id, b.id)
    assert reg is not None and reg.cancelled_at is None and reg.cancelled_by is None


def test_promotion_skips_deactivated_users(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1)
    a, b, c = factory.users(3)
    for s in (a, b, c):
        put(client, factory, event, s)
    with SessionLocal() as db:
        db.execute(update(User).where(User.id == b.id).values(is_active=False))
        db.commit()
    delete(client, factory, event, a)
    assert registration_of(event.id, b.id).status == RegistrationStatus.WAITLISTED  # type: ignore[union-attr]
    assert registration_of(event.id, c.id).status == RegistrationStatus.CONFIRMED  # type: ignore[union-attr]


def test_capacity_decrease_below_occupancy_is_rejected(client: TestClient, factory: Factory) -> None:
    """Spec §14 test 9."""
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser, capacity=3)
    for s in factory.users(3):
        put(client, factory, event, s)
    response = client.patch(f"{API}/events/{event.id}", json={"capacity": 2}, headers=factory.auth(organiser))
    assert response.status_code == 409 and error_code(response) == "CAPACITY_BELOW_OCCUPANCY"
    assert client.get(f"{API}/events/{event.id}").json()["capacity"] == 3


def test_capacity_increase_promotes_fifo(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser, capacity=1, waitlist_limit=5)
    students = factory.users(4)  # 1 confirmed + 3 waitlisted
    for s in students:
        put(client, factory, event, s)
    response = client.patch(f"{API}/events/{event.id}", json={"capacity": 3}, headers=factory.auth(organiser))
    assert response.status_code == 200
    assert response.json()["seats_left"] == 0 and response.json()["waitlist_count"] == 1
    statuses = [registration_of(event.id, s.id).status for s in students]  # type: ignore[union-attr]
    assert statuses == [RegistrationStatus.CONFIRMED] * 3 + [RegistrationStatus.WAITLISTED]


def test_organiser_removal_and_attendee_list(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser, capacity=1)
    a, b = factory.users(2)
    put(client, factory, event, a)
    put(client, factory, event, b)

    attendees = client.get(f"{API}/events/{event.id}/registrations", headers=factory.auth(organiser)).json()
    assert attendees["total"] == 2
    positions = {item["user_id"]: item["queue_position"] for item in attendees["items"]}
    assert positions == {str(a.id): None, str(b.id): 1}
    only_waiting = client.get(
        f"{API}/events/{event.id}/registrations", params={"status": "waitlisted"}, headers=factory.auth(organiser)
    ).json()
    assert [i["user_id"] for i in only_waiting["items"]] == [str(b.id)]

    reg_a = registration_of(event.id, a.id)
    assert reg_a is not None
    removed = client.delete(f"{API}/events/{event.id}/registrations/{reg_a.id}", headers=factory.auth(organiser))
    assert removed.status_code == 200 and removed.json()["status"] == "cancelled"
    after = registration_of(event.id, a.id)
    assert after is not None and after.cancelled_by == organiser.id
    assert registration_of(event.id, b.id).status == RegistrationStatus.CONFIRMED  # type: ignore[union-attr]


def test_my_registrations(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    student = factory.user()
    e1 = factory.event(organiser, title="First")
    e2 = factory.event(organiser, title="Second")
    put(client, factory, e1, student)
    put(client, factory, e2, student)
    mine = client.get(f"{API}/me/registrations", headers=factory.auth(student)).json()
    assert mine["total"] == 2 and {i["event_title"] for i in mine["items"]} == {"First", "Second"}
