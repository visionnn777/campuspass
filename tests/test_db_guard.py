"""Spec §14 test 12: the guard refuses anything except campuspass_test."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from tests.conftest import APP_DATABASE_URL, TEST_DATABASE_URL
from tests.db_guard import UnsafeTestDatabaseError, verify_connected_database, verify_test_database_url


def test_guard_accepts_the_test_database() -> None:
    verify_test_database_url(TEST_DATABASE_URL, APP_DATABASE_URL)


def test_guard_refuses_missing_url() -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="not set"):
        verify_test_database_url(None, APP_DATABASE_URL)


def test_guard_refuses_the_application_database() -> None:
    assert TEST_DATABASE_URL is not None
    app_like = make_url(TEST_DATABASE_URL).set(database="campuspass").render_as_string(hide_password=False)
    with pytest.raises(UnsafeTestDatabaseError, match="must point to 'campuspass_test'"):
        verify_test_database_url(app_like, None)


def test_guard_refuses_same_target_as_app() -> None:
    assert TEST_DATABASE_URL is not None
    with pytest.raises(UnsafeTestDatabaseError, match="same database"):
        verify_test_database_url(TEST_DATABASE_URL, TEST_DATABASE_URL)


def test_guard_refuses_unparseable_url() -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        verify_test_database_url("not a url at all", None)


def test_connected_database_check_rejects_the_app_database() -> None:
    assert TEST_DATABASE_URL is not None
    app_like = make_url(TEST_DATABASE_URL).set(database="campuspass")
    other = create_engine(app_like)
    try:
        with pytest.raises(UnsafeTestDatabaseError):
            verify_connected_database(other)  # either connects to 'campuspass' or cannot verify
    finally:
        other.dispose()
