"""Step 3: signup, password policy, login, tokens (spec §5)."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import User, UserRole
from tests.conftest import API, TEST_PASSWORD, Factory, error_code

GOOD_PASSWORD = "chai at nehru place four pm"


def signup(client: TestClient, **overrides: Any) -> Any:
    body = {"email": "ravi.kumar@example.com", "full_name": "Ravi Kumar", "password": GOOD_PASSWORD}
    body.update(overrides)
    return client.post(f"{API}/auth/register", json=body)


# ------------------------------------------------------------------ signup


def test_signup_creates_a_normalized_student(client: TestClient) -> None:
    response = signup(client, email="  Ravi.Kumar@Example.COM ")
    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "student" and body["is_active"] is True
    assert body["email"] == "ravi.kumar@example.com"
    assert "password_hash" not in body and "password" not in body


@pytest.mark.parametrize("field,value", [("role", "admin"), ("is_active", False), ("id", str(uuid.uuid4()))])
def test_signup_rejects_privilege_fields(client: TestClient, field: str, value: Any) -> None:
    response = signup(client, **{field: value})
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


def test_duplicate_email_is_409_even_with_different_case(client: TestClient) -> None:
    assert signup(client).status_code == 201
    response = signup(client, email="RAVI.KUMAR@example.com")
    assert response.status_code == 409
    assert error_code(response) == "EMAIL_TAKEN"


@pytest.mark.parametrize(
    "password,reason",
    [
        ("short pass", "at least 15"),
        ("123456789987654321", "commonly used"),  # from the bundled SecLists blocklist
        ("ravi likes long walks home", "must not contain"),  # contains the user's name
        ("i love campuspass so much", "must not contain"),  # contains the service name
        ("x" * 129, "at most 128"),
    ],
)
def test_password_policy(client: TestClient, password: str, reason: str) -> None:
    response = signup(client, password=password)
    assert response.status_code == 422
    issues = " ".join(d["issue"] for d in response.json()["error"]["details"])
    assert reason in issues


def test_no_composition_rules_long_passphrase_is_fine(client: TestClient) -> None:
    assert signup(client, password="all lowercase words are totally fine").status_code == 201


def test_allowed_email_domains(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "allowed_email_domains", "college.edu")
    assert signup(client, email="ravi@gmail.com").status_code == 422
    assert signup(client, email="ravi@college.edu").status_code == 201


# ------------------------------------------------------------------ login


def test_login_returns_a_bearer_token(client: TestClient, factory: Factory) -> None:
    user = factory.user()
    response = client.post(f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 30 * 60
    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["id"] == str(user.id)


def test_unknown_email_and_wrong_password_look_identical(client: TestClient, factory: Factory) -> None:
    user = factory.user()
    unknown = client.post(f"{API}/auth/login", json={"email": "nobody@example.com", "password": TEST_PASSWORD})
    wrong = client.post(f"{API}/auth/login", json={"email": user.email, "password": "wrong password here!!"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]
    assert error_code(unknown) == error_code(wrong) == "INVALID_CREDENTIALS"


def test_disabled_account_with_correct_password_is_403(client: TestClient, factory: Factory) -> None:
    user = factory.user(is_active=False)
    ok_pw = client.post(f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD})
    bad_pw = client.post(f"{API}/auth/login", json={"email": user.email, "password": "wrong password here!!"})
    assert ok_pw.status_code == 403 and error_code(ok_pw) == "ACCOUNT_DISABLED"
    assert bad_pw.status_code == 401


# ------------------------------------------------------------------ tokens


def _claims(user_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    settings = get_settings()
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + 600,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": uuid.uuid4().hex,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _sign(claims: dict[str, Any], key: str | None = None) -> str:
    return jwt.encode(claims, key or get_settings().jwt_secret, algorithm="HS256")


def _b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _me(client: TestClient, token: str) -> Any:
    return client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_missing_token_is_401_with_www_authenticate(client: TestClient) -> None:
    response = client.get(f"{API}/auth/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert error_code(response) == "NOT_AUTHENTICATED"


def test_bad_tokens_are_401(client: TestClient, factory: Factory) -> None:
    user = factory.user()
    now = int(time.time())
    bad_tokens = {
        "malformed": "not.a.jwt",
        "expired": _sign(_claims(user.id, iat=now - 7200, exp=now - 3600)),
        "wrong_issuer": _sign(_claims(user.id, iss="someone-else")),
        "wrong_audience": _sign(_claims(user.id, aud="other-api")),
        "missing_jti": _sign(_claims(user.id, jti=None)),
        "missing_exp": _sign(_claims(user.id, exp=None)),
        "sub_not_uuid": _sign(_claims(user.id, sub="admin")),
        "other_secret": _sign(_claims(user.id), key="x" * 64),
        "alg_none": f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims(user.id))}.",
        "unknown_user": _sign(_claims(uuid.uuid4())),
    }
    for name, token in bad_tokens.items():
        response = _me(client, token)
        assert response.status_code == 401, name
        assert error_code(response) == "INVALID_TOKEN", name


def test_tampered_signature_is_401(client: TestClient, factory: Factory) -> None:
    token = factory.token(factory.user())
    header, payload, signature = token.split(".")
    tampered_payload = _b64({**json.loads(base64.urlsafe_b64decode(payload + "==")), "sub": str(uuid.uuid4())})
    assert _me(client, f"{header}.{tampered_payload}.{signature}").status_code == 401


def test_deactivated_users_token_stops_working_immediately(client: TestClient, factory: Factory) -> None:
    user = factory.user()
    token = factory.token(user)
    assert _me(client, token).status_code == 200
    with SessionLocal() as db:
        db.execute(update(User).where(User.id == user.id).values(is_active=False))
        db.commit()
    assert _me(client, token).status_code == 401


def test_role_change_applies_on_the_next_request(client: TestClient, factory: Factory) -> None:
    organiser = factory.user(UserRole.ORGANISER)
    token = {"Authorization": f"Bearer {factory.token(organiser)}"}
    assert client.get(f"{API}/me/events", headers=token).status_code == 200
    with SessionLocal() as db:
        db.execute(update(User).where(User.id == organiser.id).values(role=UserRole.STUDENT))
        db.commit()
    # Same token, but the role is read from the DB on every request.
    assert client.get(f"{API}/me/events", headers=token).status_code == 403
