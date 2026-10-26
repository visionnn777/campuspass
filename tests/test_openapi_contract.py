"""Keep the committed portable OpenAPI document in sync with the live application."""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def test_exported_openapi_matches_application_paths_and_schemas() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    exported = json.loads((repo_root / "docs" / "openapi.json").read_text(encoding="utf-8"))
    generated = app.openapi()

    assert exported["openapi"] == generated["openapi"]
    assert set(exported["paths"]) == set(generated["paths"])
    assert exported["components"]["schemas"].keys() == generated["components"]["schemas"].keys()
    assert exported["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert "/api/v1/events/{event_id}/checkins" in exported["paths"]
    assert "/api/v1/events/{event_id}/registrations/me" in exported["paths"]
