"""Create the first admin (or promote an existing account): the break-glass path (spec §5).

Usage:  python -m scripts.create_admin

The password is read with getpass: it is not echoed and never lands in shell history.
"""

from __future__ import annotations

import getpass
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password, load_password_blocklist, password_policy_issues
from app.models import User, UserRole


def _ask(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        sys.exit("Aborted: a value is required.")
    return value


def main() -> None:
    load_password_blocklist()
    try:
        email = str(TypeAdapter(EmailStr).validate_python(_ask("Admin email: "))).strip().lower()
    except ValidationError:
        sys.exit("Aborted: that is not a valid email address.")

    with SessionLocal() as db:
        existing = db.scalars(select(User).where(User.email == email)).one_or_none()
        if existing is not None:
            answer = input(f"{email} already exists (role: {existing.role}). Make it an active admin? [y/N] ")
            if answer.strip().lower() != "y":
                sys.exit("Aborted.")
            existing.role = UserRole.ADMIN
            existing.is_active = True
            db.commit()
            print(f"OK: {email} is now an active admin.")
            return

        full_name = _ask("Full name: ")[:100]
        password = getpass.getpass("Password (15-128 characters, a phrase is best): ")
        issues = password_policy_issues(password, email=email, full_name=full_name)
        if issues:
            sys.exit("Aborted: password " + "; ".join(issues))
        if getpass.getpass("Repeat password: ") != password:
            sys.exit("Aborted: passwords do not match.")

        db.add(
            User(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        db.commit()
        print(f"OK: admin {email} created.")


if __name__ == "__main__":
    main()
