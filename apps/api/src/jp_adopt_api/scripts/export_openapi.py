"""Write OpenAPI schema to apps/api/openapi.json for packages/contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jp_adopt_api.main import app


def main() -> None:
    out = Path(__file__).resolve().parents[3] / "openapi.json"
    schema = app.openapi()
    out.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
    sys.exit(0)
