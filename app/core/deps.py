"""Authentication dependencies (spec §5).

* HTTP Bearer tokens (not the OAuth2 password grant, which RFC 9700 forbids).
* The user is loaded from the database on EVERY request, so a role change or
  deactivation takes effect on the very next request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import AppError
from app.core.security import decode_access_token
from app.models import User, UserRole

# auto_error=False: we raise our own 401 in the standard error shape.
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Paste the access_token returned by POST /api/v1/auth/login",
)

_WWW_AUTH = {"WWW-Authenticate": "Bearer"}


def _unauthorized(code: str, message: str) -> AppError:
    return AppError(401, code, message, headers=_WWW_AUTH)


def _load_user(request: Request, db: Session, token: str) -> User:
    try:
        user_id = decode_access_token(token)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise _unauthorized("INVALID_TOKEN", "The access token is invalid or expired.") from None

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("INVALID_TOKEN", "The access token is invalid or expired.")

    # End the implicit read transaction, so each service can open its own
    # `with session.begin():` transaction. expire_on_commit=False keeps `user` loaded.
    db.commit()
    request.state.user_id = str(user.id)  # for the access log
    return user


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("NOT_AUTHENTICATED", "Log in and send 'Authorization: Bearer <token>'.")
    return _load_user(request, db, credentials.credentials)


def get_optional_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    """For public endpoints that show more to owners (e.g. drafts). A bad token is still a 401."""
    if credentials is None:
        return None
    return _load_user(request, db, credentials.credentials)


def require_roles(*roles: UserRole) -> Callable[..., User]:
    """Dependency factory: the current user must have one of `roles`, else 403."""

    def dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise AppError(403, "FORBIDDEN", "You do not have permission to do this.")
        return user

    return dependency


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
DbSession = Annotated[Session, Depends(get_db)]
OrganiserOrAdmin = Annotated[User, Depends(require_roles(UserRole.ORGANISER, UserRole.ADMIN))]
AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
