"""Seed predictable demo identities and an event for the interview walkthrough.

Usage:
    python -m scripts.seed_demo
    python -m scripts.seed_demo --reset-demo-accounts
    python -m scripts.seed_demo --new-event

Default behavior preserves credentials, roles, and active flags for existing accounts.
`--reset-demo-accounts` explicitly resets ONLY the six hard-coded demo identities and is
allowed in APP_ENV=development only. `--new-event` intentionally creates a fresh event;
otherwise the current published demo event is reused when it has not ended.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import now_utc
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import Event, EventCategory, EventStatus, User, UserRole

DEMO_PASSWORD = "demo passphrase for local testing"  # development-only fixture credential
DEMO_EVENT_TITLE = "Demo: Intro to Backend Engineering"
ORGANISER = ("organiser@demo.campuspass.dev", "Priya Organiser")
STUDENTS = [(f"student{i}@demo.campuspass.dev", f"Demo Student {i}") for i in range(1, 6)]


@dataclass(frozen=True)
class AccountSeedResult:
    user: User
    created: bool
    password_set_by_this_run: bool


def _get_or_create(
    db: Session,
    email: str,
    name: str,
    role: UserRole,
    password_hash: str,
    *,
    reset_existing: bool,
) -> AccountSeedResult:
    """Create missing demo users; modify existing ones only under an explicit reset flag."""
    user = db.scalars(select(User).where(User.email == email)).one_or_none()
    if user is None:
        user = User(
            email=email,
            full_name=name,
            password_hash=password_hash,
            role=role,
            is_active=True,
        )
        db.add(user)
        db.flush()
        return AccountSeedResult(user=user, created=True, password_set_by_this_run=True)

    if reset_existing:
        # This code can only target the hard-coded demo emails above, and the flag is
        # rejected outside development mode. Never reset arbitrary users' credentials.
        user.full_name = name
        user.password_hash = password_hash
        user.role = role
        user.is_active = True
        db.flush()
        return AccountSeedResult(user=user, created=False, password_set_by_this_run=True)

    # Default path deliberately preserves passwords, roles, names, and active state.
    return AccountSeedResult(user=user, created=False, password_set_by_this_run=False)


def _create_demo_event(db: Session, organiser: User, title: str, t) -> Event:
    """Create a new, published event whose registration and check-in windows are open."""
    event = Event(
        organiser_id=organiser.id,
        title=title,
        description="Seeded demo event: capacity 2, waitlist 2.",
        category=EventCategory.TECH,
        venue="Seminar Hall 1",
        registration_opens_at=t - timedelta(minutes=10),
        registration_closes_at=t + timedelta(minutes=50),
        starts_at=t + timedelta(hours=1),
        ends_at=t + timedelta(hours=3),
        capacity=2,
        waitlist_limit=2,
        status=EventStatus.PUBLISHED,
    )
    db.add(event)
    db.flush()
    return event


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create CampusPass demo accounts and event.")
    parser.add_argument(
        "--reset-demo-accounts",
        action="store_true",
        help="Explicitly reset password, role, name, and active flag for the six known demo accounts (development only).",
    )
    parser.add_argument(
        "--new-event",
        action="store_true",
        help="Create a fresh demo event instead of reusing the latest unended published demo event.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.app_env == "production":
        sys.exit("Refusing to seed demo data when APP_ENV=production.")
    if args.reset_demo_accounts and settings.app_env != "development":
        sys.exit("--reset-demo-accounts is allowed only when APP_ENV=development.")

    password_hash = hash_password(DEMO_PASSWORD)
    t = now_utc().replace(microsecond=0)
    with SessionLocal() as db:
        with db.begin():
            organiser_result = _get_or_create(
                db, *ORGANISER, UserRole.ORGANISER, password_hash,
                reset_existing=args.reset_demo_accounts,
            )
            student_results = [
                _get_or_create(
                    db, email, name, UserRole.STUDENT, password_hash,
                    reset_existing=args.reset_demo_accounts,
                )
                for email, name in STUDENTS
            ]
            results = [organiser_result, *student_results]

            # If old accounts are incompatible, do not create an event no one can manage
            # and do not silently change the existing account to make the demo work.
            incompatible = [
                result.user.email
                for result in results
                if not result.user.is_active
                or result.user.role
                != (UserRole.ORGANISER if result.user.email == ORGANISER[0] else UserRole.STUDENT)
            ]
            if incompatible:
                raise SystemExit(
                    "Existing demo accounts have an unexpected role or are disabled: "
                    + ", ".join(incompatible)
                    + ". Their data was not changed. In development, rerun with --reset-demo-accounts."
                )

            existing_event = None
            if not args.new_event:
                existing_event = db.scalars(
                    select(Event)
                    .where(
                        Event.organiser_id == organiser_result.user.id,
                        Event.title == DEMO_EVENT_TITLE,
                        Event.status == EventStatus.PUBLISHED,
                        Event.ends_at > t,
                    )
                    .order_by(Event.created_at.desc(), Event.id.desc())
                    .limit(1)
                ).one_or_none()
            event = existing_event or _create_demo_event(db, organiser_result.user, DEMO_EVENT_TITLE, t)

        # The transaction committed successfully before we print credentials or IDs.
        created_emails = [r.user.email for r in results if r.created]
        reset_emails = [r.user.email for r in results if not r.created and r.password_set_by_this_run]
        preserved_emails = [r.user.email for r in results if not r.password_set_by_this_run]

    print("Demo data ready.")
    print(f"  Event id : {event.id}")
    print(f"  Event    : {event.title}")
    print(f"  Event reused: {'yes' if existing_event is not None else 'no'}")
    print(f"  Organiser: {ORGANISER[0]}")
    print(f"  Students : {', '.join(email for email, _ in STUDENTS)}")
    if created_emails or reset_emails:
        print(f"  DEMO_PASSWORD is guaranteed for accounts created/reset by this run: {DEMO_PASSWORD!r}")
        if created_emails:
            print(f"  Created accounts: {', '.join(created_emails)}")
        if reset_emails:
            print(f"  Reset accounts  : {', '.join(reset_emails)}")
    if preserved_emails:
        print("  Existing credentials were preserved and were not verified for: " + ", ".join(preserved_emails))
    if existing_event is not None:
        print("  Existing event reused. Use --new-event to create a fresh event for another walkthrough.")


if __name__ == "__main__":
    main()
