# tripl frontend

React 19 + TypeScript + Vite, styled with Tailwind v4. Setup, scripts and the
review gates live in [CONTRIBUTING.md](../CONTRIBUTING.md); this file covers
the generated API types and points at the lint setup.

## API types (generated from the backend OpenAPI schema)

`src/types/api.gen.ts` is **auto-generated** by
[`openapi-typescript`](https://openapi-ts.dev) from the committed backend
schema snapshot at `../backend/openapi.json`. Do not edit it by hand.

Regenerate it whenever the backend API surface changes (the backend
`test_openapi_contract.py` test fails until `backend/openapi.json` is refreshed,
which is your cue to re-run this):

```bash
# 1. Refresh the backend snapshot (run from the backend/ directory)
uv run python -c "import json; from tripl.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > openapi.json

# 2. Regenerate the frontend types (run from the frontend/ directory)
bun run gen:api
```

### Incremental adoption

The existing hand-written types under `src/types/` remain the source of truth
for now. `api.gen.ts` is added alongside them as a drift-guarded reference so we
can migrate module-by-module. To adopt a generated type, import from the
generated `paths`/`components` instead of the hand-written file, e.g.:

```ts
import type { components } from '@/types/api.gen'

type MetricResponse = components['schemas']['MetricResponse']
```

Do this one domain at a time and delete the corresponding hand-written type once
its consumers are migrated and the build is green. There is no need to convert
everything at once.

## Linting

Oxlint is the only linter; `bun run lint` runs it together with the project rule
tests, with zero warnings allowed. See the **Linting** section of
[CONTRIBUTING.md](../CONTRIBUTING.md#linting).
