"""/api/v1/auth: signup, JSON login (no OAuth2 password grant), current user. Spec §5."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbSession
from app.core.ratelimit import enforce, hashed_key, ip_limit
from app.core.security import create_access_token
from app.schemas.auth import LoginIn, SignupIn, TokenOut, UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    dependencies=[ip_limit("register", "5/hour")],
    summary="Create a student account",
)
def register(data: SignupIn, db: DbSession) -> UserOut:
    return UserOut.model_validate(auth_service.signup(db, data))


@router.post(
    "/login",
    response_model=TokenOut,
    dependencies=[ip_limit("login-ip", "10/minute")],
    summary="Log in with email and password (JSON) and get a bearer token",
)
def login(data: LoginIn, db: DbSession) -> TokenOut:
    # Per-email soft throttle (spec §6): slows guessing against one account from many IPs.
    enforce("login-email", "10/15 minutes", hashed_key(data.email))
    user = auth_service.authenticate(db, data)
    token, expires_in = create_access_token(user.id)
    return TokenOut(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserOut, summary="The logged-in user")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
