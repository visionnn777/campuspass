"""One error shape for every non-2xx response, plus the exception handlers (spec §12).

    {"error": {"code": "EVENT_FULL", "message": "...", "request_id": "..."}}

Handlers never expose stack traces, SQL, secrets, tokens or the submitted input.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("campuspass")


class AppError(Exception):
    """A deliberate, documented API error (business conflict, auth failure, ...)."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.details = details


def get_request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def error_response(
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        body["details"] = details
    all_headers = {"X-Request-ID": request_id, **(headers or {})}
    return JSONResponse(status_code=status_code, content={"error": body}, headers=all_headers)


# Codes for framework-raised HTTP errors (unknown route, wrong method, ...).
_HTTP_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "NOT_AUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    411: "LENGTH_REQUIRED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(
        get_request_id(request),
        exc.status_code,
        exc.code,
        exc.message,
        headers=exc.headers,
        details=exc.details,
    )


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_response(
        get_request_id(request), exc.status_code, code, message, headers=dict(exc.headers or {})
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Only the location and the reason are returned, NEVER the submitted value:
    # a validation error on a password field must not echo the password back.
    details = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ()) if part != "body") or "body",
            "issue": str(err.get("msg", "invalid value")),
        }
        for err in exc.errors()
    ]
    return error_response(
        get_request_id(request),
        422,
        "VALIDATION_ERROR",
        "The request is invalid.",
        details=details,
    )


async def _db_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, (DBAPIError, PoolTimeoutError))
    # Imported here to avoid a circular import (db_errors builds AppError objects).
    from app.core.db_errors import map_db_error

    request_id = get_request_id(request)
    app_error, level = map_db_error(exc)
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    # Log the SQLSTATE and constraint name only: never the SQL text or parameters.
    logger.log(
        level,
        "database error mapped",
        extra={
            "request_id": request_id,
            "sqlstate": sqlstate,
            "constraint": constraint,
            "error_code": app_error.code,
        },
    )
    return error_response(
        request_id,
        app_error.status_code,
        app_error.code,
        app_error.message,
        headers=app_error.headers,
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id(request)
    logger.error(
        "unhandled exception",
        extra={"request_id": request_id, "exception_type": type(exc).__name__},
        exc_info=exc,
    )
    return error_response(request_id, 500, "INTERNAL_ERROR", "An unexpected error occurred.")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(DBAPIError, _db_error_handler)
    # SQLAlchemy pool exhaustion raises sqlalchemy.exc.TimeoutError, not DBAPIError.
    app.add_exception_handler(PoolTimeoutError, _db_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
