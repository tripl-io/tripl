#!/usr/bin/env python3
"""Fail when the docs site's API reference is not backend/openapi.json.

    check-openapi-docs.py [SNAPSHOT DOCS]

bin/sync-api-types.sh (`make sync-types`) writes both documents from one
app.openapi() call: backend/openapi.json with sorted keys, which the backend's
contract test pins to the live app, and website/openapi/tripl.openapi.json in
the app's own order, which Redoc renders as the API reference on the docs site.
They are compared parsed, so key order does not count; any other difference
means the docs copy was edited by hand or not regenerated with the snapshot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "backend" / "openapi.json"
DOCS = ROOT / "website" / "openapi" / "tripl.openapi.json"
USAGE = "usage: check-openapi-docs.py [SNAPSHOT DOCS]"


def differences(snapshot: dict[str, Any], docs: dict[str, Any]) -> list[str]:
    """Each path and schema that differs by name, or else the top-level keys."""
    named: list[str] = []
    for label, ours, theirs in (
        ("path", snapshot.get("paths", {}), docs.get("paths", {})),
        (
            "schema",
            snapshot.get("components", {}).get("schemas", {}),
            docs.get("components", {}).get("schemas", {}),
        ),
    ):
        named += [
            f"{label} {name}"
            for name in sorted(ours.keys() | theirs.keys())
            if ours.get(name) != theirs.get(name)
        ]
    return named or [
        f"top-level {key}"
        for key in sorted(snapshot.keys() | docs.keys())
        if snapshot.get(key) != docs.get(key)
    ]


def main(argv: list[str]) -> int:
    if len(argv) == 2:
        snapshot_path, docs_path = Path(argv[0]), Path(argv[1])
    elif not argv:
        snapshot_path, docs_path = SNAPSHOT, DOCS
    else:
        print(USAGE, file=sys.stderr)
        return 2
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    docs = json.loads(docs_path.read_text(encoding="utf-8"))
    if docs == snapshot:
        print(f"{docs_path} matches {snapshot_path}")
        return 0
    print(
        f"{docs_path} differs from {snapshot_path}. Regenerate both with `make sync-types`:",
        file=sys.stderr,
    )
    for difference in differences(snapshot, docs):
        print(f"  {difference}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
