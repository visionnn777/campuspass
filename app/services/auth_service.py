"""Signup and login (spec §5)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import dummy_password_hash, hash_password, verify_password
from app.models import User, UserRole
from app.schemas.auth import LoginIn, SignupIn


def signup(db: Session, data: SignupIn) -> User:
    """Create a STUDENT account. A duplicate email surfaces as uq_users_email -> 409 EMAIL_TAKEN.

    No "SELECT first, then INSERT": two simultaneous signups could both pass the SELECT.
    The unique constraint decides, and db_errors.py maps it precisely.
    """
    password_hash = hash_password(data.password)  # slow Argon2 work happens BEFORE the transaction
    with db.begin():
        user = User(
            email=data.email,
            full_name=data.full_name,
            password_hash=password_hash,
            role=UserRole.STUDENT,  # always: clients cannot choose their own role
            is_active=True,
        )
        db.add(user)
        db.flush()
    return user


def authenticate(db: Session, data: LoginIn) -> User:
    """Same 401 for unknown email and wrong password, with equal timing (dummy hash)."""
    user = db.scalars(select(User).where(User.email == data.email)).one_or_none()
    db.commit()  # end the read transaction before the slow hash check

    invalid = AppError(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")
    if user is None:
        verify_password(data.password, dummy_password_hash())  # burn the same time
        raise invalid
    if not verify_password(data.password, user.password_hash):
        raise invalid
    if not user.is_active:
        # Only reachable with the CORRECT password, so it reveals nothing to a guesser.
        raise AppError(403, "ACCOUNT_DISABLED", "This account has been disabled.")
    return user
