"""The business clock (spec §2, §8 Rule 3).

Every time-based business rule (registration window, check-in window, "has the
event started?") reads `now_utc()` *after* taking its locks.

Why not SQL `now()`? In PostgreSQL, `now()` is the time the *transaction started*.
A request that began at 09:59:59 and waited for a lock until 10:00:01 would still
see 09:59:59 and slip past a 10:00 deadline.

Tests replace the provider with a fixed or offset clock so that window edges can be
tested exactly. JWT expiry deliberately uses the real clock (PyJWT checks real time).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

_provider: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731


def now_utc() -> datetime:
    """Current business time, always timezone-aware UTC."""
    return _provider()


def set_clock(provider: Callable[[], datetime]) -> None:
    """Replace the clock (used by tests only)."""
    global _provider
    _provider = provider


def reset_clock() -> None:
    """Restore the real clock."""
    set_clock(lambda: datetime.now(UTC))
