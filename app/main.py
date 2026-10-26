"""CampusPass application factory.

Run:  uvicorn app.main:app --reload      (one worker: in-memory rate limits, spec §6)
Docs: http://127.0.0.1:8000/docs

The app NEVER creates or drops tables: the schema comes only from `alembic upgrade head`.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware, configure_logging
from app.core.security import load_password_blocklist
from app.routers import admin, auth, checkins, events, health, me, registrations, tickets

API_PREFIX = "/api/v1"

DESCRIPTION = """
Event registration API that cannot overbook.

* **Lock protocol:** every operation that depends on an event's state locks the event row
  first (`FOR UPDATE` to change it, `FOR SHARE` for check-in), then registration rows.
* **FIFO waitlist** with automatic promotion in the same transaction as a cancellation.
* **Versioned, signed tickets:** each ticket checks in exactly once.

**Try it:** `POST /api/v1/auth/login` → copy `access_token` → click **Authorize** → paste it.
"""


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast if .env is missing or unsafe
    load_password_blocklist()  # fails fast if the blocklist file is missing
    configure_logging()

    app = FastAPI(title="CampusPass API", version="1.0.0", description=DESCRIPTION)
    register_exception_handlers(app)

    # add_middleware wraps from the inside out: the LAST added runs FIRST.
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.max_body_bytes)
    app.add_middleware(RequestContextMiddleware)  # outermost: every response gets X-Request-ID

    app.include_router(health.router)
    for router in (
        auth.router,
        admin.router,
        events.router,
        registrations.router,
        tickets.router,
        checkins.router,
        me.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
