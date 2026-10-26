"""Demo-seeding regression tests: idempotent identities and explicit credential reset."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.core.security import hash_password, verify_password
from app.models import Event, User, UserRole
from scripts import seed_demo


def _configure_seed_env(monkeypatch: pytest.MonkeyPatch, env: str = "development") -> None:
    # The test database remains configured by tests/conftest.py; this only controls the
    # seed script's explicit development/production safety decision.
    monkeypatch.setattr(seed_demo, "get_settings", lambda: SimpleNamespace(app_env=env))


def _user(email: str) -> User | None:
    with SessionLocal() as db:
        return db.scalars(select(User).where(User.email == email)).one_or_none()


def test_seed_is_idempotent_for_accounts_and_reuses_current_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_seed_env(monkeypatch)
    seed_demo.main([])
    with SessionLocal() as db:
        users_after_first_run = db.scalar(select(func.count()).select_from(User))
        events_after_first_run = db.scalar(select(func.count()).select_from(Event))
        organiser = db.scalars(select(User).where(User.email == seed_demo.ORGANISER[0])).one()
        original_hash = organiser.password_hash

    seed_demo.main([])
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == users_after_first_run == 6
        assert db.scalar(select(func.count()).select_from(Event)) == events_after_first_run == 1
        organiser = db.scalars(select(User).where(User.email == seed_demo.ORGANISER[0])).one()
        assert organiser.password_hash == original_hash  # default behavior preserves credentials


def test_existing_incompatible_demo_account_is_not_silently_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_seed_env(monkeypatch)
    existing_password = "a distinct existing password phrase"
    with SessionLocal() as db:
        user = User(
            email=seed_demo.ORGANISER[0],
            full_name="Existing Student",
            password_hash=hash_password(existing_password),
            role=UserRole.STUDENT,
            is_active=True,
        )
        db.add(user)
        db.commit()
        existing_id = user.id
        existing_hash = user.password_hash

    with pytest.raises(SystemExit, match="--reset-demo-accounts"):
        seed_demo.main([])

    with SessionLocal() as db:
        user = db.get(User, existing_id)
        assert user is not None
        assert user.role == UserRole.STUDENT
        assert user.password_hash == existing_hash
        assert verify_password(existing_password, user.password_hash)
        assert db.scalar(select(func.count()).select_from(Event)) == 0
        # Missing demo users created earlier in the failed transaction were rolled back.
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_explicit_reset_repairs_only_known_demo_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_seed_env(monkeypatch)
    wrong_password = "another unique existing long passphrase"
    with SessionLocal() as db:
        db.add(
            User(
                email=seed_demo.ORGANISER[0],
                full_name="Wrong Existing Name",
                password_hash=hash_password(wrong_password),
                role=UserRole.STUDENT,
                is_active=False,
            )
        )
        db.commit()

    seed_demo.main(["--reset-demo-accounts"])
    with SessionLocal() as db:
        users = db.scalars(select(User)).all()
        assert len(users) == 6
        for user in users:
            expected_role = UserRole.ORGANISER if user.email == seed_demo.ORGANISER[0] else UserRole.STUDENT
            assert user.role == expected_role and user.is_active
            assert verify_password(seed_demo.DEMO_PASSWORD, user.password_hash)


def test_seed_refuses_production_and_resets_are_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_seed_env(monkeypatch, "production")
    with pytest.raises(SystemExit, match="APP_ENV=production"):
        seed_demo.main([])
    with pytest.raises(SystemExit, match="APP_ENV=production"):
        seed_demo.main(["--reset-demo-accounts"])

    _configure_seed_env(monkeypatch, "test")
    with pytest.raises(SystemExit, match="development"):
        seed_demo.main(["--reset-demo-accounts"])
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(Event)) == 0
