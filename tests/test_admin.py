"""Step 3: admin endpoints and guards (spec §5). The concurrent last-admin race is test 14."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.models import UserRole
from tests.conftest import API, Factory, error_code


def test_only_admins_can_list_users(client: TestClient, factory: Factory) -> None:
    admin = factory.user(UserRole.ADMIN)
    student = factory.user()
    assert client.get(f"{API}/admin/users", headers=factory.auth(student)).status_code == 403
    response = client.get(f"{API}/admin/users?role=student", headers=factory.auth(admin))
    assert response.status_code == 200
    assert [u["id"] for u in response.json()["items"]] == [str(student.id)]


def test_admin_promotes_a_student_to_organiser(client: TestClient, factory: Factory) -> None:
    admin = factory.user(UserRole.ADMIN)
    student = factory.user()
    response = client.patch(
        f"{API}/admin/users/{student.id}", json={"role": "organiser"}, headers=factory.auth(admin)
    )
    assert response.status_code == 200 and response.json()["role"] == "organiser"
    # The promotion is effective immediately for the student's existing token.
    assert client.get(f"{API}/me/events", headers=factory.auth(student)).status_code == 200


def test_admin_cannot_demote_or_disable_self(client: TestClient, factory: Factory) -> None:
    admin = factory.user(UserRole.ADMIN)
    factory.user(UserRole.ADMIN)  # a second admin exists, so only the self-guard applies
    for body in ({"role": "student"}, {"is_active": False}):
        response = client.patch(f"{API}/admin/users/{admin.id}", json=body, headers=factory.auth(admin))
        assert response.status_code == 409
        assert error_code(response) == "CANNOT_MODIFY_SELF"


def test_admin_can_disable_another_admin_when_two_exist(client: TestClient, factory: Factory) -> None:
    admin_a = factory.user(UserRole.ADMIN)
    admin_b = factory.user(UserRole.ADMIN)
    response = client.patch(
        f"{API}/admin/users/{admin_b.id}", json={"is_active": False}, headers=factory.auth(admin_a)
    )
    assert response.status_code == 200 and response.json()["is_active"] is False


def test_admin_update_validation(client: TestClient, factory: Factory) -> None:
    admin = factory.user(UserRole.ADMIN)
    target = factory.user()
    url = f"{API}/admin/users/{target.id}"
    assert client.patch(url, json={}, headers=factory.auth(admin)).status_code == 422
    assert client.patch(url, json={"role": None}, headers=factory.auth(admin)).status_code == 422
    assert client.patch(url, json={"email": "x@example.com"}, headers=factory.auth(admin)).status_code == 422
    missing = client.patch(f"{API}/admin/users/{uuid.uuid4()}", json={"role": "organiser"}, headers=factory.auth(admin))
    assert missing.status_code == 404 and error_code(missing) == "USER_NOT_FOUND"
