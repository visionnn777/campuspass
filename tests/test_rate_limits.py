"""Spec §14 test 11: each limit triggers at the right count, for the right KEY."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.models import UserRole
from tests.conftest import API, Factory, error_code


@pytest.fixture(autouse=True)
def _limits_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "rate_limits_enabled", True)
    yield


def assert_limited(response) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == 429, response.text
    assert error_code(response) == "RATE_LIMITED"
    assert int(response.headers["Retry-After"]) >= 1


def test_signup_is_limited_per_ip(client: TestClient) -> None:
    for i in range(5):
        body = {"email": f"u{i}@example.com", "full_name": "Rate Test", "password": "a long and unusual phrase"}
        assert client.post(f"{API}/auth/register", json=body).status_code == 201
    sixth = {"email": "u6@example.com", "full_name": "Rate Test", "password": "a long and unusual phrase"}
    assert_limited(client.post(f"{API}/auth/register", json=sixth))
    other_ip = TestClient(app, client=("10.0.0.9", 5000))
    assert other_ip.post(f"{API}/auth/register", json=sixth).status_code == 201


def test_login_is_limited_per_ip(client: TestClient) -> None:
    for i in range(10):
        body = {"email": f"nobody{i}@example.com", "password": "wrong password here!!"}
        assert client.post(f"{API}/auth/login", json=body).status_code == 401
    assert_limited(client.post(f"{API}/auth/login", json={"email": "x@example.com", "password": "whatever"}))


def test_login_is_limited_per_email_across_ips(factory: Factory) -> None:
    victim = factory.user()
    for i in range(10):
        attacker = TestClient(app, client=(f"10.1.0.{i}", 5000))  # a new IP each time
        attacker.post(f"{API}/auth/login", json={"email": victim.email, "password": "guess guess guess"})
    fresh_ip = TestClient(app, client=("10.2.0.1", 5000))
    assert_limited(fresh_ip.post(f"{API}/auth/login", json={"email": victim.email, "password": "guess"}))


def test_registration_is_limited_per_account_not_per_ip(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER))
    alice, bob = factory.users(2)
    url = f"{API}/events/{event.id}/registrations/me"
    for _ in range(10):
        assert client.put(url, headers=factory.auth(alice)).status_code in (200, 201)
    assert_limited(client.put(url, headers=factory.auth(alice)))
    # Same IP, different account: separate counter.
    assert client.put(url, headers=factory.auth(bob)).status_code == 201


def test_registration_is_also_limited_per_ip(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER), capacity=100)
    students = factory.users(61)
    url = f"{API}/events/{event.id}/registrations/me"
    for student in students[:60]:
        assert client.put(url, headers=factory.auth(student)).status_code == 201
    assert_limited(client.put(url, headers=factory.auth(students[60])))


def test_cancellation_is_limited_per_account(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER))
    student = factory.user()
    url = f"{API}/events/{event.id}/registrations/me"
    client.put(url, headers=factory.auth(student))
    for _ in range(10):
        assert client.delete(url, headers=factory.auth(student)).status_code == 200
    assert_limited(client.delete(url, headers=factory.auth(student)))


def test_ticket_view_and_reissue_limits(client: TestClient, factory: Factory) -> None:
    event = factory.event(factory.user(UserRole.ORGANISER))
    student = factory.user()
    headers = factory.auth(student)
    client.put(f"{API}/events/{event.id}/registrations/me", headers=headers)
    view = f"{API}/events/{event.id}/registrations/me/ticket"
    for _ in range(30):
        assert client.get(view, headers=headers).status_code == 200
    assert_limited(client.get(view, headers=headers))
    for _ in range(5):
        assert client.post(f"{view}/reissue", headers=headers).status_code == 200
    assert_limited(client.post(f"{view}/reissue", headers=headers))


def test_checkin_scanner_limit(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    event = factory.event(organiser)
    headers = factory.auth(organiser)
    url = f"{API}/events/{event.id}/checkins"
    for _ in range(120):
        assert client.post(url, json={"token": "garbage"}, headers=headers).status_code == 404
    assert_limited(client.post(url, json={"token": "garbage"}, headers=headers))


def test_public_event_reads_are_limited_per_ip(client: TestClient) -> None:
    for _ in range(120):
        assert client.get(f"{API}/events").status_code == 200
    assert_limited(client.get(f"{API}/events"))
