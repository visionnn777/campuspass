"""ASGI middleware: request IDs, request-size limit and a JSON access log (spec §6, §12).

Written as plain ASGI middleware (not BaseHTTPMiddleware) so request bodies are
never buffered by the middleware itself.

Never logged: the Authorization header, request bodies, passwords, tokens, secrets.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response

logger = logging.getLogger("campuspass")

_LOG_FIELDS = (
    "request_id",
    "method",
    "route",
    "status",
    "duration_ms",
    "user_id",
    "sqlstate",
    "constraint",
    "error_code",
    "exception_type",
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to search and safe to ship to a log service."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for field in _LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    if any(getattr(h, "_campuspass", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._campuspass = True  # type: ignore[attr-defined]  # marks our handler (idempotent setup)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class RequestContextMiddleware:
    """Assigns a request ID, echoes it in X-Request-ID, and writes one access-log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        # Reuse a client-supplied ID only if it is a valid UUID (prevents log injection).
        request_id = incoming if incoming and _valid_uuid(incoming) else str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    # The route TEMPLATE (/events/{event_id}), not the raw path.
                    "route": getattr(route, "path", None) or "unmatched",
                    "status": status_holder["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "user_id": state.get("user_id"),
                },
            )


class BodySizeLimitMiddleware:
    """Rejects oversized bodies BEFORE they are read into memory (spec §6).

    Pydantic length checks run only after the whole body is parsed, so they cannot
    stop a 500 MB request. This can.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(scope.get("state", {}).get("request_id", "unknown"))
        content_length = _header(scope, b"content-length")
        transfer_encoding = (_header(scope, b"transfer-encoding") or "").lower()

        if content_length is None and "chunked" in transfer_encoding:
            await error_response(
                request_id, 411, "LENGTH_REQUIRED", "A Content-Length header is required."
            )(scope, receive, send)
            return
        if content_length is not None:
            if not content_length.isdigit():
                await error_response(
                    request_id, 400, "BAD_REQUEST", "Invalid Content-Length header."
                )(scope, receive, send)
                return
            if int(content_length) > self.max_body_bytes:
                await error_response(
                    request_id,
                    413,
                    "PAYLOAD_TOO_LARGE",
                    f"Request body must be at most {self.max_body_bytes} bytes.",
                )(scope, receive, send)
                return

        # Defence in depth: also count the bytes actually received.
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await error_response(
                request_id,
                413,
                "PAYLOAD_TOO_LARGE",
                f"Request body must be at most {self.max_body_bytes} bytes.",
            )(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None
