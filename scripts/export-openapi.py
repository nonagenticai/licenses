#!/usr/bin/env python3
"""Export OpenAPI spec from the FastAPI app to docs/openapi.json.

The environment the spec is built under lives in ``scripts/openapi_env.py`` and
is shared with ``tests/unit/test_openapi_drift.py`` — see that module for why
the values are forced rather than ``setdefault``-ed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.openapi_env import apply_export_env  # noqa: E402

# Must happen before `main` is imported: main reads SERVICE_NAME and VERSION at
# module scope and bakes them into the document.
apply_export_env()

from main import app  # noqa: E402

DEFAULT_OUT = ROOT / "docs" / "openapi.json"


def write_spec(out: Path) -> Path:
    """Render the OpenAPI document to ``out`` and return the path written."""
    spec = app.openapi()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = DEFAULT_OUT
    if args:
        if args[0] == "--out" and len(args) == 2:
            out = Path(args[1]).resolve()
        else:
            print(f"usage: {Path(__file__).name} [--out PATH]", file=sys.stderr)
            return 2
    write_spec(out)
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
