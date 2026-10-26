"""Export the application's current OpenAPI document to docs/openapi.json.

Run from the repository root after installing the project and configuring `.env`:
    python -m scripts.export_openapi

This imports the real FastAPI application and serializes `app.openapi()`; it does not
hand-maintain a second API contract. Re-run it whenever routes or schemas change.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def main() -> None:
    output = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    schema = app.openapi()
    output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(schema.get('paths', {}))} paths to {output.relative_to(output.parents[1])}")


if __name__ == "__main__":
    main()
