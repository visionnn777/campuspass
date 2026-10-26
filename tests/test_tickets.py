"""Step 7: tickets and check-in (spec §11)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.core.database import SessionLocal
from app.models import Event, RegistrationStatus, User, UserRole
from tests.conftest import API, Factory, TestClock, error_code, registration_of


def setup_confirmed(client: TestClient, factory: Factory) -> tuple[User, Event, User]:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser)
    student = factory.user(name="Asha Verma")
    assert client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(student)).status_code == 201
    return organiser, event, student


def ticket(client: TestClient, factory: Factory, event: Event, user: User) -> str:
    response = client.get(f"{API}/events/{event.id}/registrations/me/ticket", headers=factory.auth(user))
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def check_in(client: TestClient, factory: Factory, event: Event, scanner: User, token: str):  # type: ignore[no-untyped-def]
    return client.post(f"{API}/events/{event.id}/checkins", json={"token": token}, headers=factory.auth(scanner))


def open_checkin(clock: TestClock, event: Event) -> None:
    clock.set(event.starts_at - timedelta(hours=1))


def test_viewing_is_repeatable_and_png_matches(client: TestClient, factory: Factory) -> None:
    _, event, student = setup_confirmed(client, factory)
    first = ticket(client, factory, event, student)
    assert ticket(client, factory, event, student) == first  # viewing never rotates
    assert first.startswith("cp1.") and len(first) < 128
    png = client.get(
        f"{API}/events/{event.id}/registrations/me/ticket", params={"format": "png"}, headers=factory.auth(student)
    )
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content.startswith(b"\x89PNG")
    assert ticket(client, factory, event, student) == first  # the PNG view didn't rotate it either




def test_admin_can_get_ticket_for_an_event_they_created(client: TestClient, factory: Factory) -> None:
    admin = factory.user(UserRole.ADMIN)
    event = factory.event(admin, capacity=1)
    response = client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(admin))
    assert response.status_code == 201
    ticket_response = client.get(
        f"{API}/events/{event.id}/registrations/me/ticket", headers=factory.auth(admin)
    )
    assert ticket_response.status_code == 200


def test_organiser_cannot_use_old_registration_for_their_own_event(
    client: TestClient, factory: Factory, clock: TestClock
) -> None:
    admin = factory.user(UserRole.ADMIN)
    owner = factory.user(UserRole.ORGANISER)
    event = factory.event(owner, capacity=1)

    # Simulate an authorised admin changing roles: the former organiser becomes a student,
    # registers, then is made organiser again. The old registration must not bypass policy.
    changed = client.patch(
        f"{API}/admin/users/{owner.id}", json={"role": "student"}, headers=factory.auth(admin)
    )
    assert changed.status_code == 200
    registration = client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(owner))
    assert registration.status_code == 201
    token = ticket(client, factory, event, owner)

    changed_back = client.patch(
        f"{API}/admin/users/{owner.id}", json={"role": "organiser"}, headers=factory.auth(admin)
    )
    assert changed_back.status_code == 200
    view = client.get(f"{API}/events/{event.id}/registrations/me/ticket", headers=factory.auth(owner))
    reissue = client.post(
        f"{API}/events/{event.id}/registrations/me/ticket/reissue", headers=factory.auth(owner)
    )
    assert view.status_code == 403
    assert reissue.status_code == 403

    clock.set(event.starts_at - timedelta(hours=1))
    scan = check_in(client, factory, event, admin, token)
    assert scan.status_code == 404 and error_code(scan) == "INVALID_TICKET"


def test_ticket_only_for_confirmed(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=1)
    a, b, c = factory.users(3)
    client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(a))
    client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(b))  # waitlisted
    waiting = client.get(f"{API}/events/{event.id}/registrations/me/ticket", headers=factory.auth(b))
    assert waiting.status_code == 409 and error_code(waiting) == "TICKET_NOT_AVAILABLE"
    none = client.get(f"{API}/events/{event.id}/registrations/me/ticket", headers=factory.auth(c))
    assert none.status_code == 404


def test_check_in_once_then_409(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    token = ticket(client, factory, event, student)
    open_checkin(clock, event)
    first = check_in(client, factory, event, organiser, token)
    assert first.status_code == 200 and first.json()["attendee_name"] == "Asha Verma"
    second = check_in(client, factory, event, organiser, token)
    assert second.status_code == 409 and error_code(second) == "ALREADY_CHECKED_IN"
    reg = registration_of(event.id, student.id)
    assert reg is not None and reg.status == RegistrationStatus.CHECKED_IN and reg.checked_in_by == organiser.id


def test_check_in_window(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    token = ticket(client, factory, event, student)
    clock.set(event.starts_at - timedelta(hours=2, seconds=1))
    assert error_code(check_in(client, factory, event, organiser, token)) == "CHECKIN_NOT_OPEN"
    clock.set(event.ends_at + timedelta(seconds=1))
    assert error_code(check_in(client, factory, event, organiser, token)) == "CHECKIN_NOT_OPEN"
    clock.set(event.starts_at - timedelta(hours=2))  # exactly when it opens
    assert check_in(client, factory, event, organiser, token).status_code == 200


def test_invalid_tickets_are_404(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    other_event = factory.event(organiser)
    token = ticket(client, factory, event, student)
    open_checkin(clock, event)
    prefix, payload, mac = token.split(".")
    tampered_mac = mac[:-2] + ("AA" if not mac.endswith("AA") else "BB")
    b64_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    # Change only unused pad bits in the final Base64URL character. A permissive decoder
    # can decode this to the same payload, but it is not our one canonical token encoding.
    last_index = b64_alphabet.index(payload[-1])
    noncanonical_payload = payload[:-1] + b64_alphabet[last_index ^ 1]
    malformed = (
        "garbage",
        "cp1.abc.def",
        f"{prefix}.{payload}.{tampered_mac}",
        f"cp2.{payload}.{mac}",
        f"{prefix}.{payload}=.{mac}",  # padding is forbidden in this wire format
        f"{prefix}.{payload}.{mac}=",
        f"{prefix}.{payload}.{mac}.extra",
        f"{prefix}.{noncanonical_payload}.{mac}",
        f"{prefix}.{payload}*.{mac}",
    )
    for bad in malformed:
        response = check_in(client, factory, event, organiser, bad)
        assert response.status_code == 404 and error_code(response) == "INVALID_TICKET", bad
    # A valid ticket for another event is invalid here (event_id is in the lookup).
    wrong_event = check_in(client, factory, other_event, organiser, token)
    assert wrong_event.status_code == 404 and error_code(wrong_event) == "INVALID_TICKET"


def test_reissue_kills_old_token(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    old = ticket(client, factory, event, student)
    reissued = client.post(f"{API}/events/{event.id}/registrations/me/ticket/reissue", headers=factory.auth(student))
    assert reissued.status_code == 200 and reissued.json()["ticket_version"] == 2
    new = reissued.json()["token"]
    assert new != old and ticket(client, factory, event, student) == new
    open_checkin(clock, event)
    assert check_in(client, factory, event, organiser, old).status_code == 404
    assert check_in(client, factory, event, organiser, new).status_code == 200


def test_cancel_kills_ticket_and_reconfirmation_does_not_revive_it(
    client: TestClient, factory: Factory, clock: TestClock
) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    old = ticket(client, factory, event, student)
    client.delete(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(student))
    client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(student))  # confirmed again
    new = ticket(client, factory, event, student)
    assert new != old
    open_checkin(clock, event)
    assert check_in(client, factory, event, organiser, old).status_code == 404  # spec Appendix B8
    assert check_in(client, factory, event, organiser, new).status_code == 200


def test_cancelled_event_blocks_check_in_and_reissue(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    token = ticket(client, factory, event, student)
    client.post(f"{API}/events/{event.id}/cancel", headers=factory.auth(organiser))
    open_checkin(clock, event)
    response = check_in(client, factory, event, organiser, token)
    assert response.status_code == 409 and error_code(response) == "EVENT_NOT_ACTIVE"
    reissue = client.post(f"{API}/events/{event.id}/registrations/me/ticket/reissue", headers=factory.auth(student))
    assert reissue.status_code == 409 and error_code(reissue) == "TICKET_NOT_AVAILABLE"


def test_disabled_attendee_is_rejected(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    token = ticket(client, factory, event, student)
    with SessionLocal() as db:
        db.execute(update(User).where(User.id == student.id).values(is_active=False))
        db.commit()
    open_checkin(clock, event)
    response = check_in(client, factory, event, organiser, token)
    assert response.status_code == 409 and error_code(response) == "ATTENDEE_DISABLED"


def test_cannot_cancel_after_check_in(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    token = ticket(client, factory, event, student)
    open_checkin(clock, event)
    check_in(client, factory, event, organiser, token)
    response = client.delete(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(student))
    assert response.status_code == 409 and error_code(response) == "CANNOT_CANCEL_AFTER_CHECKIN"


def test_who_may_check_in(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser, event, student = setup_confirmed(client, factory)
    token = ticket(client, factory, event, student)
    open_checkin(clock, event)
    assert check_in(client, factory, event, student, token).status_code == 403  # students: role check
    other_organiser = factory.user(UserRole.ORGANISER)
    response = check_in(client, factory, event, other_organiser, token)
    assert response.status_code == 403 and error_code(response) == "FORBIDDEN"
    admin = factory.user(UserRole.ADMIN)
    assert check_in(client, factory, event, admin, token).status_code == 200
