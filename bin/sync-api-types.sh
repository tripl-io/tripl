#!/usr/bin/env bash
# Regenerate everything derived from the backend's OpenAPI schema, from one
# app.openapi() call (`make sync-types`):
#
#   1. backend/openapi.json — the committed snapshot that
#      tests/test_openapi_contract.py pins byte for byte and `bun run gen:api`
#      reads: json.dumps(app.openapi(), indent=2, sort_keys=True) and a
#      trailing newline.
#   2. website/openapi/tripl.openapi.json — the same document in the app's own
#      order, which the docs site renders as its API reference. Redoc lists
#      operations in document order, so sorted keys would put every tag's
#      operations in alphabetical order. CI runs bin/check-openapi-docs.py,
#      which fails when this copy says anything (1) does not.
#   3. frontend/src/types/api.gen.ts, via `bun run gen:api`.
#
# Run after any change to the HTTP API surface (routes, request/response models,
# status codes). Requires the backend's deps (`cd backend && uv sync`) and bun.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ dumping the OpenAPI schema to backend/openapi.json and website/openapi/tripl.openapi.json"
(
  cd "$root/backend"
  uv run python - "$root/backend/openapi.json" "$root/website/openapi/tripl.openapi.json" <<'PY'
import json
import sys

from tripl.main import app

snapshot, docs = sys.argv[1:]
spec = app.openapi()
for path, sort_keys in ((snapshot, True), (docs, False)):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(spec, indent=2, sort_keys=sort_keys) + "\n")
PY
)

echo "→ regenerating frontend/src/types/api.gen.ts"
(
  cd "$root/frontend"
  bun run gen:api
)

echo "✓ API types in sync (backend/openapi.json, website/openapi/tripl.openapi.json, frontend/src/types/api.gen.ts)"
