"""Permission matrix (spec §5, §14): every denial leaves the data unchanged."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import EventStatus, UserRole
from tests.conftest import API, Factory, TestClock, error_code, load_event, registration_of


def test_organiser_cannot_touch_another_organisers_event(client: TestClient, factory: Factory) -> None:
    owner = factory.user(UserRole.ORGANISER)
    intruder = factory.user(UserRole.ORGANISER)
    event = factory.event(owner, title="Owner's Event")
    headers = factory.auth(intruder)
    attempts = [
        client.patch(f"{API}/events/{event.id}", json={"title": "Hijacked"}, headers=headers),
        client.post(f"{API}/events/{event.id}/cancel", headers=headers),
        client.delete(f"{API}/events/{event.id}", headers=headers),
        client.get(f"{API}/events/{event.id}/registrations", headers=headers),
    ]
    for response in attempts:
        assert response.status_code == 403 and error_code(response) == "FORBIDDEN"
    after = load_event(event.id)
    assert after.title == "Owner's Event" and after.status == EventStatus.PUBLISHED


def test_someone_elses_draft_is_404_not_403(client: TestClient, factory: Factory) -> None:
    owner = factory.user(UserRole.ORGANISER)
    intruder = factory.user(UserRole.ORGANISER)
    draft = factory.event(owner, status=EventStatus.DRAFT)
    headers = factory.auth(intruder)
    for response in (
        client.get(f"{API}/events/{draft.id}", headers=headers),
        client.patch(f"{API}/events/{draft.id}", json={"title": "x" * 10}, headers=headers),
        client.post(f"{API}/events/{draft.id}/publish", headers=headers),
        client.delete(f"{API}/events/{draft.id}", headers=headers),
    ):
        assert response.status_code == 404 and error_code(response) == "EVENT_NOT_FOUND"
    assert load_event(draft.id).status == EventStatus.DRAFT


def test_students_cannot_use_organiser_or_admin_routes(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    student = factory.user()
    victim = factory.user()
    event = factory.event(organiser)
    client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(victim))
    reg = registration_of(event.id, victim.id)
    assert reg is not None
    headers = factory.auth(student)
    for response in (
        client.post(f"{API}/events/{event.id}/publish", headers=headers),
        client.get(f"{API}/events/{event.id}/registrations", headers=headers),
        client.delete(f"{API}/events/{event.id}/registrations/{reg.id}", headers=headers),
        client.get(f"{API}/admin/users", headers=headers),
        client.patch(f"{API}/admin/users/{student.id}", json={"role": "admin"}, headers=headers),
        client.get(f"{API}/me/events", headers=headers),
    ):
        assert response.status_code == 403
    assert registration_of(event.id, victim.id).status == reg.status  # type: ignore[union-attr]


def test_admin_can_manage_any_event(client: TestClient, factory: Factory) -> None:
    owner = factory.user(UserRole.ORGANISER)
    admin = factory.user(UserRole.ADMIN)
    event = factory.event(owner)
    response = client.patch(f"{API}/events/{event.id}", json={"venue": "Main Auditorium"}, headers=factory.auth(admin))
    assert response.status_code == 200 and response.json()["venue"] == "Main Auditorium"


def test_other_organiser_cannot_check_in_at_my_event(client: TestClient, factory: Factory, clock: TestClock) -> None:
    owner = factory.user(UserRole.ORGANISER)
    other = factory.user(UserRole.ORGANISER)
    student = factory.user()
    event = factory.event(owner)
    client.put(f"{API}/events/{event.id}/registrations/me", headers=factory.auth(student))
    token = client.get(f"{API}/events/{event.id}/registrations/me/ticket", headers=factory.auth(student)).json()["token"]
    clock.set(event.starts_at - timedelta(hours=1))
    response = client.post(f"{API}/events/{event.id}/checkins", json={"token": token}, headers=factory.auth(other))
    assert response.status_code == 403
    reg = registration_of(event.id, student.id)
    assert reg is not None and reg.checked_in_at is None
