"""Step 5: events (spec §7)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.models import EventCategory, EventStatus, UserRole
from tests.conftest import API, Factory, TestClock, error_code, load_event


def event_body(clock: TestClock, **overrides: Any) -> dict[str, Any]:
    t = clock.now

    def iso(delta: timedelta) -> str:
        return (t + delta).isoformat()

    body: dict[str, Any] = {
        "title": "Intro to Databases",
        "description": "Bring a laptop.",
        "category": "tech",
        "venue": "Seminar Hall 2",
        "registration_opens_at": iso(timedelta(hours=-1)),
        "registration_closes_at": iso(timedelta(hours=1)),
        "starts_at": iso(timedelta(hours=3)),
        "ends_at": iso(timedelta(hours=5)),
        "capacity": 30,
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ create


def test_organiser_creates_a_draft(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    response = client.post(f"{API}/events", json=event_body(clock), headers=factory.auth(organiser))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft" and body["waitlist_limit"] == 50
    assert body["organiser_id"] == str(organiser.id) and body["seats_left"] == 30


def test_students_and_anonymous_cannot_create(client: TestClient, factory: Factory, clock: TestClock) -> None:
    student = factory.user()
    assert client.post(f"{API}/events", json=event_body(clock), headers=factory.auth(student)).status_code == 403
    assert client.post(f"{API}/events", json=event_body(clock)).status_code == 401


def test_create_validation(client: TestClient, factory: Factory, clock: TestClock) -> None:
    headers = factory.auth(factory.user(UserRole.ORGANISER))
    naive = (clock.now + timedelta(hours=3)).replace(tzinfo=None).isoformat()
    cases = [
        event_body(clock, starts_at=naive),  # no timezone offset
        event_body(clock, ends_at=(clock.now + timedelta(hours=2)).isoformat()),  # ends before start
        event_body(clock, status="published"),  # status is not client-settable
        event_body(clock, organiser_id="00000000-0000-0000-0000-000000000000"),
        event_body(clock, capacity=0),
        event_body(clock, category="party"),
    ]
    for body in cases:
        assert client.post(f"{API}/events", json=body, headers=headers).status_code == 422


def test_event_must_start_in_the_future(client: TestClient, factory: Factory, clock: TestClock) -> None:
    headers = factory.auth(factory.user(UserRole.ORGANISER))
    t = clock.now
    body = event_body(
        clock,
        registration_opens_at=(t - timedelta(hours=5)).isoformat(),
        registration_closes_at=(t - timedelta(hours=4)).isoformat(),
        starts_at=(t - timedelta(hours=3)).isoformat(),
        ends_at=(t + timedelta(hours=1)).isoformat(),
    )
    response = client.post(f"{API}/events", json=body, headers=headers)
    assert response.status_code == 422 and error_code(response) == "INVALID_EVENT_TIMES"


# ------------------------------------------------------------------ visibility


def test_drafts_are_invisible_to_everyone_but_owner_and_admin(client: TestClient, factory: Factory) -> None:
    owner = factory.user(UserRole.ORGANISER)
    other = factory.user(UserRole.ORGANISER)
    admin = factory.user(UserRole.ADMIN)
    draft = factory.event(owner, status=EventStatus.DRAFT)
    url = f"{API}/events/{draft.id}"
    assert client.get(url).status_code == 404
    assert client.get(url, headers=factory.auth(other)).status_code == 404
    assert client.get(url, headers=factory.auth(owner)).status_code == 200
    assert client.get(url, headers=factory.auth(admin)).status_code == 200
    assert client.get(f"{API}/events").json()["total"] == 0
    mine = client.get(f"{API}/me/events", headers=factory.auth(owner)).json()
    assert [e["id"] for e in mine["items"]] == [str(draft.id)]


# ------------------------------------------------------------------ listing


def test_search_treats_wildcards_literally(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    factory.event(organiser, title="100% Fun Night")
    factory.event(organiser, title="1000 Fun Night")
    factory.event(organiser, title="snake_case workshop")
    factory.event(organiser, title="snakeXcase workshop")

    def titles(search: str) -> list[str]:
        return [e["title"] for e in client.get(f"{API}/events", params={"search": search}).json()["items"]]

    assert titles("%") == ["100% Fun Night"]
    assert titles("_") == ["snake_case workshop"]
    assert set(titles("fun")) == {"1000 Fun Night", "100% Fun Night"}


def test_filters_sort_pagination_and_past_events(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    early = factory.event(organiser, title="Early", starts=timedelta(hours=2), ends=timedelta(hours=3))
    late = factory.event(
        organiser, title="Late", category=EventCategory.SPORTS, starts=timedelta(hours=8), ends=timedelta(hours=9)
    )
    factory.event(organiser, title="Cancelled one", status=EventStatus.CANCELLED)

    listing = client.get(f"{API}/events", params={"sort": "-starts_at"}).json()
    assert listing["total"] == 3
    assert listing["items"][0]["id"] == str(late.id)

    sports = client.get(f"{API}/events", params={"category": "sports"}).json()
    assert [e["id"] for e in sports["items"]] == [str(late.id)]

    page = client.get(f"{API}/events", params={"page": 2, "page_size": 2, "sort": "starts_at"}).json()
    assert page["page"] == 2 and len(page["items"]) == 1 and page["total"] == 3

    assert client.get(f"{API}/events", params={"page_size": 101}).status_code == 422
    assert client.get(f"{API}/events", params={"page": 0}).status_code == 422
    assert client.get(f"{API}/events", params={"sort": "title; DROP TABLE"}).status_code == 422

    clock.advance(hours=4)  # "Early" has ended
    ids = [e["id"] for e in client.get(f"{API}/events").json()["items"]]
    assert str(early.id) not in ids
    with_past = client.get(f"{API}/events", params={"include_past": True}).json()["items"]
    assert str(early.id) in [e["id"] for e in with_past]


# ------------------------------------------------------------------ lifecycle


def test_publish_rules(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    draft = factory.event(organiser, status=EventStatus.DRAFT)
    url = f"{API}/events/{draft.id}/publish"
    response = client.post(url, headers=factory.auth(organiser))
    assert response.status_code == 200 and response.json()["status"] == "published"
    again = client.post(url, headers=factory.auth(organiser))
    assert again.status_code == 409 and error_code(again) == "INVALID_STATE"

    late_draft = factory.event(organiser, status=EventStatus.DRAFT)
    clock.advance(hours=1)  # registration has now closed
    late = client.post(f"{API}/events/{late_draft.id}/publish", headers=factory.auth(organiser))
    assert late.status_code == 409 and error_code(late) == "REGISTRATION_CLOSED"


def test_cancel_rules(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    headers = factory.auth(organiser)
    draft = factory.event(organiser, status=EventStatus.DRAFT)
    assert error_code(client.post(f"{API}/events/{draft.id}/cancel", headers=headers)) == "INVALID_STATE"

    event = factory.event(organiser)
    response = client.post(f"{API}/events/{event.id}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled" and response.json()["cancelled_at"] is not None
    assert error_code(client.post(f"{API}/events/{event.id}/cancel", headers=headers)) == "INVALID_STATE"

    ended = factory.event(organiser)
    clock.advance(hours=6)
    assert error_code(client.post(f"{API}/events/{ended.id}/cancel", headers=headers)) == "EVENT_ENDED"


def test_delete_only_drafts(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    headers = factory.auth(organiser)
    draft = factory.event(organiser, status=EventStatus.DRAFT)
    published = factory.event(organiser)
    assert client.delete(f"{API}/events/{draft.id}", headers=headers).status_code == 204
    assert client.get(f"{API}/events/{draft.id}", headers=headers).status_code == 404
    response = client.delete(f"{API}/events/{published.id}", headers=headers)
    assert response.status_code == 409 and error_code(response) == "INVALID_STATE"


def test_patch_rules(client: TestClient, factory: Factory, clock: TestClock) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    headers = factory.auth(organiser)
    event = factory.event(organiser)
    url = f"{API}/events/{event.id}"

    ok = client.patch(url, json={"title": "Advanced Databases", "venue": "Lab 3"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["title"] == "Advanced Databases"

    assert client.patch(url, json={}, headers=headers).status_code == 422
    assert client.patch(url, json={"title": None}, headers=headers).status_code == 422
    assert client.patch(url, json={"status": "cancelled"}, headers=headers).status_code == 422

    bad_order = client.patch(url, json={"ends_at": (clock.now + timedelta(hours=2)).isoformat()}, headers=headers)
    assert bad_order.status_code == 422 and error_code(bad_order) == "INVALID_EVENT_TIMES"

    clock.advance(hours=3)  # the event has started
    started = client.patch(url, json={"title": "Too late"}, headers=headers)
    assert started.status_code == 409 and error_code(started) == "EVENT_STARTED"
    assert load_event(event.id).title == "Advanced Databases"


def test_cancelled_events_are_read_only(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser, status=EventStatus.CANCELLED)
    response = client.patch(f"{API}/events/{event.id}", json={"title": "Revived"}, headers=factory.auth(organiser))
    assert response.status_code == 409 and error_code(response) == "INVALID_STATE"


def test_event_out_snapshot_fields(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser, capacity=2)
    body = client.get(f"{API}/events/{event.id}").json()
    assert body["is_registration_open"] is True and body["is_completed"] is False
    assert body["seats_left"] == 2 and body["waitlist_count"] == 0
    assert body["organiser_name"] == organiser.full_name
    assert datetime.fromisoformat(body["starts_at"]).tzinfo is not None
