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

### Using the generated types

`api.gen.ts` is the source of truth for API payloads. The modules under
`src/types/` (and the request types in `src/api/`) name them for the app, as
aliases of the generated schemas rather than restatements:

```ts
import type { components } from './api.gen'

type Schemas = components['schemas']

export type AlertRule = Schemas['AlertRuleResponse']
```

`metrics`, `alerting`, `branches`, `serviceSettings` and most of `scans` are
aliased this way. Intersect (`&`) only for a genuine client-side refinement,
such as typing a `result_summary` the schema serves as an untyped dict.

openapi-typescript marks a field the backend declares with a `None` default as
optional (`field?: T | null`) even though FastAPI always sends it, so read such
a field with `== null` or `??`, never `=== null`.

Most hand-written types that are not aliased yet have an entry in
`src/types/apiDrift.ts`, which fails `tsc` when their field names stop matching
the generated schema; a few still differ from it and have none. Replace such a
type with an alias (and drop its entry) when you touch its domain.

## Linting

Oxlint is the only linter; `bun run lint` runs it together with the project rule
tests, with zero warnings allowed. See the **Linting** section of
[CONTRIBUTING.md](../CONTRIBUTING.md#linting).
