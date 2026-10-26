"""Test-database safety guard (spec §14). The tests TRUNCATE every table, so this
must make it impossible to point them at the real database by accident.

All conditions must hold, otherwise the run stops (fail closed):
1. TEST_DATABASE_URL is set. There is NO fallback to DATABASE_URL.
2. Its parsed database name is exactly `campuspass_test`.
3. Its (host, port, database) differ from DATABASE_URL's.
4. After connecting, SELECT current_database() returns `campuspass_test`.
"""

from __future__ import annotations

from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

EXPECTED_TEST_DATABASE = "campuspass_test"


class UnsafeTestDatabaseError(RuntimeError):
    pass


def verify_test_database_url(test_url: str | None, app_url: str | None) -> None:
    if not test_url:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is not set (there is no fallback).")
    try:
        parsed_test = make_url(test_url)
    except Exception as exc:  # noqa: BLE001 - any parse problem means "unsafe"
        raise UnsafeTestDatabaseError(f"TEST_DATABASE_URL cannot be parsed: {type(exc).__name__}") from None
    if parsed_test.database != EXPECTED_TEST_DATABASE:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL must point to '{EXPECTED_TEST_DATABASE}', not '{parsed_test.database}'."
        )
    if app_url:
        try:
            parsed_app = make_url(app_url)
        except Exception:  # noqa: BLE001
            raise UnsafeTestDatabaseError("DATABASE_URL cannot be parsed; refusing to continue.") from None
        same_target = (parsed_test.host, parsed_test.port, parsed_test.database) == (
            parsed_app.host,
            parsed_app.port,
            parsed_app.database,
        )
        if same_target:
            raise UnsafeTestDatabaseError("TEST_DATABASE_URL points to the same database as DATABASE_URL.")


def verify_connected_database(engine: Engine) -> None:
    try:
        with engine.connect() as conn:
            name = conn.execute(text("SELECT current_database()")).scalar_one()
    except Exception as exc:  # noqa: BLE001 - cannot verify => unsafe
        raise UnsafeTestDatabaseError(f"Could not verify the test database: {type(exc).__name__}") from None
    if name != EXPECTED_TEST_DATABASE:
        raise UnsafeTestDatabaseError(f"Connected to '{name}', expected '{EXPECTED_TEST_DATABASE}'.")
