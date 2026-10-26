"""Step 2: error shape, request IDs, body-size limit, no input echo (spec §6, §12)."""

from __future__ import annotations

import uuid

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import register_exception_handlers
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from tests.conftest import API, error_code


def test_every_response_gets_a_request_id(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    uuid.UUID(response.headers["X-Request-ID"])


def test_valid_incoming_request_id_is_reused(client: TestClient) -> None:
    rid = str(uuid.uuid4())
    assert client.get("/health", headers={"X-Request-ID": rid}).headers["X-Request-ID"] == rid


def test_invalid_incoming_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "evil\nlog-injection"})
    assert response.headers["X-Request-ID"] != "evil\nlog-injection"
    uuid.UUID(response.headers["X-Request-ID"])


def test_unknown_route_uses_standard_error_shape(client: TestClient) -> None:
    response = client.get(f"{API}/does-not-exist")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "NOT_FOUND"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_wrong_method_is_405(client: TestClient) -> None:
    assert error_code(client.delete("/health")) == "METHOD_NOT_ALLOWED"


def test_oversized_body_is_rejected_before_parsing(client: TestClient) -> None:
    response = client.post(f"{API}/auth/login", content=b"x" * 70_000, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert error_code(response) == "PAYLOAD_TOO_LARGE"


def test_chunked_body_without_length_is_411(client: TestClient) -> None:
    def chunks():  # type: ignore[no-untyped-def]
        yield b'{"email": "a@example.com",'
        yield b'"password": "x"}'

    response = client.post(f"{API}/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 411
    assert error_code(response) == "LENGTH_REQUIRED"


def test_validation_errors_never_echo_the_submitted_password(client: TestClient) -> None:
    secret = "my-very-secret-pw"
    response = client.post(
        f"{API}/auth/register",
        json={"email": "not-an-email", "full_name": "A", "password": secret},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert error_code(response) == "VALIDATION_ERROR"
    assert {d["field"] for d in response.json()["error"]["details"]} >= {"email"}


def test_unhandled_exception_returns_500_without_internals() -> None:
    mini = FastAPI()
    register_exception_handlers(mini)
    mini.add_middleware(BodySizeLimitMiddleware, max_body_bytes=1024)
    mini.add_middleware(RequestContextMiddleware)

    @mini.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail: password=hunter2")

    response = TestClient(mini, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert error_code(response) == "INTERNAL_ERROR"
    assert "hunter2" not in response.text and "secret internal" not in response.text
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_health_ready_reports_migration(client: TestClient) -> None:
    # Compare with Alembic's head instead of hard-coding a revision id, so adding a
    # migration never silently breaks this test.
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    body = client.get("/health/ready").json()
    assert body == {"status": "ok", "db": "ok", "migration": head}
