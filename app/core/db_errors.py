"""Precise database-error mapping (spec §12).

The session has already been rolled back (services use `with session.begin():`,
and get_db() rolls back on any exception) before this runs.

Rules:
* Map by SQLSTATE and *constraint name*, never by "it was an IntegrityError".
* Unknown constraint violations are bugs -> 500, never a business conflict.
* Timeouts and deadlocks are temporary -> 503 BUSY with Retry-After.
* A database outage never becomes a business conflict.

Note: lock/statement timeouts and deadlocks arrive as sqlalchemy OperationalError,
not IntegrityError, which is why everything is keyed on `sqlstate`.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError as PoolTimeoutError

from app.core.errors import AppError

UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"
LOCK_NOT_AVAILABLE = "55P03"  # lock_timeout expired
QUERY_CANCELED = "57014"  # statement_timeout expired
DEADLOCK_DETECTED = "40P01"

# Known constraint -> (API error, log level).
_KNOWN_CONSTRAINTS: dict[tuple[str, str], tuple[AppError, int]] = {
    (UNIQUE_VIOLATION, "uq_users_email"): (
        AppError(409, "EMAIL_TAKEN", "An account with this email already exists."),
        logging.INFO,
    ),
    (UNIQUE_VIOLATION, "uq_registrations_event_id"): (
        # Cannot happen while the lock protocol holds; logged as an error because it means a bug.
        AppError(409, "ALREADY_REGISTERED", "You are already registered for this event."),
        logging.ERROR,
    ),
    (CHECK_VIOLATION, "ck_events_time_order"): (
        # Defence in depth: the service validates time order before writing.
        AppError(422, "INVALID_EVENT_TIMES", "Event times are not in a valid order."),
        logging.WARNING,
    ),
}


def _busy() -> AppError:
    return AppError(
        503,
        "BUSY",
        "The server is busy with other requests for this event. Please retry.",
        headers={"Retry-After": "1"},
    )


def _internal() -> AppError:
    return AppError(500, "INTERNAL_ERROR", "An unexpected error occurred.")


def map_db_error(exc: DBAPIError | PoolTimeoutError) -> tuple[AppError, int]:
    """Return (API error, log level) for a driver or SQLAlchemy pool exception."""
    # SQLAlchemy's pool TimeoutError is not a DBAPIError and has no SQLSTATE. It means
    # the app could not acquire a connection before pool_timeout expired, not bad input.
    if isinstance(exc, PoolTimeoutError):
        return _busy(), logging.WARNING

    orig = exc.orig
    sqlstate: str | None = getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    constraint: str | None = getattr(diag, "constraint_name", None)

    if sqlstate is None:
        # No server error code: the connection itself failed (server down, network, ...).
        if isinstance(exc, OperationalError) or exc.connection_invalidated:
            return (
                AppError(503, "DATABASE_UNAVAILABLE", "The database is temporarily unavailable."),
                logging.ERROR,
            )
        return _internal(), logging.ERROR

    if constraint is not None and (sqlstate, constraint) in _KNOWN_CONSTRAINTS:
        error, level = _KNOWN_CONSTRAINTS[(sqlstate, constraint)]
        return AppError(error.status_code, error.code, error.message, headers=error.headers), level

    if sqlstate.startswith("23"):
        # Integrity violation we did not plan for: a bug, not a user conflict.
        return _internal(), logging.ERROR
    if sqlstate in (LOCK_NOT_AVAILABLE, QUERY_CANCELED):
        return _busy(), logging.WARNING
    if sqlstate == DEADLOCK_DETECTED:
        # Should be impossible with the event-first lock order: log loudly.
        return _busy(), logging.ERROR
    if sqlstate.startswith("08"):
        return (
            AppError(503, "DATABASE_UNAVAILABLE", "The database is temporarily unavailable."),
            logging.ERROR,
        )
    return _internal(), logging.ERROR
