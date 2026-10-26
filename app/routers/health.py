"""Health endpoints (spec §12). They never reveal connection strings or credentials."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import engine

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness: the process is running (no database call)")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness: database reachable and migrations applied")
def ready() -> JSONResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": "error", "migration": None})
    if revision is None:
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": "ok", "migration": None})
    return JSONResponse(status_code=200, content={"status": "ok", "db": "ok", "migration": revision})
