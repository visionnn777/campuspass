"""Rate limiting with EXPLICIT keys (spec §6), using the `limits` library.

Each limit names its key: client IP, account (user id) or login email.
Account-keyed limits depend on get_current_user, so they run AFTER authentication.

Phase 1 limitation: MemoryStorage lives inside one process. Counters reset on restart
and are not shared between workers, so the app runs with ONE worker. Phase 3 swaps
the storage for Redis (one line) with a fail-closed policy for login/signup.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable

from fastapi import Depends, Request
from limits import parse, storage, strategies

from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.errors import AppError
from app.models import User

_storage = storage.MemoryStorage()
_limiter = strategies.MovingWindowRateLimiter(_storage)


def reset_rate_limits() -> None:
    """Forget all counters (used by tests)."""
    global _storage, _limiter
    _storage = storage.MemoryStorage()
    _limiter = strategies.MovingWindowRateLimiter(_storage)


def enforce(scope: str, limit: str, key: str) -> None:
    """Count one hit for (scope, key). Raise 429 with Retry-After when over the limit."""
    if not get_settings().rate_limits_enabled:  # only possible when APP_ENV=test
        return
    item = parse(limit)
    if _limiter.hit(item, scope, key):
        return
    stats = _limiter.get_window_stats(item, scope, key)
    retry_after = max(1, math.ceil(stats.reset_time - time.time()))
    raise AppError(
        429,
        "RATE_LIMITED",
        "Too many requests. Please slow down.",
        headers={"Retry-After": str(retry_after)},
    )


def client_ip(request: Request) -> str:
    """The TCP peer address. X-Forwarded-For is honoured ONLY when uvicorn is started with
    --proxy-headers --forwarded-allow-ips=<exact proxy IP> (Phase 2 deployment)."""
    return request.client.host if request.client else "unknown"


def hashed_key(value: str) -> str:
    """Keys such as emails are hashed so raw emails are not kept in limiter memory."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def limit_by_ip(scope: str, limit: str) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        enforce(scope, limit, client_ip(request))

    return dependency


def limit_by_user(scope: str, limit: str) -> Callable[[User], None]:
    def dependency(user: CurrentUser) -> None:
        enforce(scope, limit, str(user.id))

    return dependency


def ip_limit(scope: str, limit: str) -> object:
    """Shortcut for `dependencies=[ip_limit(...)]` in route decorators."""
    return Depends(limit_by_ip(scope, limit))


def user_limit(scope: str, limit: str) -> object:
    return Depends(limit_by_user(scope, limit))
