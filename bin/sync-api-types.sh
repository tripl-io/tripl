#!/usr/bin/env bash
# Regenerate the frontend TypeScript API types from the live backend schema.
#
# One command for the two-step flow that keeps the FE <-> BE contract in sync:
#   1. Dump the OpenAPI schema to backend/openapi.json — the committed snapshot
#      that tests/test_openapi_contract.py guards and `bun run gen:api` consumes.
#      The format MUST match the contract test: json.dumps(app.openapi(),
#      indent=2, sort_keys=True) plus the single trailing newline `print` adds.
#   2. Regenerate frontend/src/types/api.gen.ts via `bun run gen:api`.
#
# Run after any change to the HTTP API surface (routes, request/response models,
# status codes). Requires backend deps (`cd backend && uv sync`).
#
# NOTE: distinct from bin/dump-openapi.sh, which writes the *docs-site* spec
# (website/openapi/tripl.openapi.json) in a different format.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ dumping OpenAPI schema to backend/openapi.json"
(
  cd "$root/backend"
  uv run python -c "import json; from tripl.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > openapi.json
)

echo "→ regenerating frontend/src/types/api.gen.ts"
(
  cd "$root/frontend"
  bun run gen:api
)

echo "✓ API types in sync (backend/openapi.json + frontend/src/types/api.gen.ts)"
