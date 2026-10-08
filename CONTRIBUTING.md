# Contributing to tripl

Thanks for working on **tripl** — an analytics tracking-plan and data-quality
monitoring service. This guide covers how to get a local environment running,
the day-to-day backend and frontend workflows, how database migrations work,
where new code belongs, and the conventions we expect on pull requests.

The agent-facing navigation map (domain model, API map, async pipeline map,
"where to look first") lives in
[AGENTS.md](https://github.com/tripl-io/tripl/blob/main/AGENTS.md). This file
is the human contributor guide. The repo's
[CLAUDE.md](https://github.com/tripl-io/tripl/blob/main/CLAUDE.md) links here
for build and test commands instead of duplicating them, so keep the command
sections below accurate.

## Prerequisites

| Tool | Version | Used for |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | latest | Backend Python env, deps, and task runner |
| Python | 3.14 (pinned in `backend/.python-version`) | Backend runtime — `uv` will fetch it for you |
| [Bun](https://bun.com/) | `1.4.2` (pinned via `packageManager`) | Frontend and docs deps, scripts and runtime |
| Node.js | `>=26 <27` (pinned in `frontend/.node-version`) | Only the project lint-rule tests in `bun run lint`: Oxlint's RuleTester refuses any other runtime |
| Docker + Compose v2 | recent | Local dev stack |

The repo pins the package managers, so use **`uv`** for the backend and
**`bun`** for the frontend and the docs site. Do **not** use `pip`, `poetry`,
`npm`, `pnpm` or `yarn` — they bypass `uv.lock` / `bun.lock` and CI will diverge
from your machine.

Install the pinned Bun:

```bash
curl -fsSL https://bun.com/install | bash -s "bun-v1.4.2"
```

:::note Warehouses are external
tripl reads from *external* analytics warehouses (ClickHouse, BigQuery,
Databricks, Snowflake, Redshift, Greenplum, Trino, Athena, and the Postgres warehouse adapter). Compose does **not** start a warehouse for you. The
PostgreSQL container in the dev stack is tripl's own system-of-record database,
not a scan target. Scans and data-source connection tests need a reachable
external warehouse.
:::

## Local Development with Docker Compose

The dev stack builds from source, runs as root, and hot-reloads via Docker
Compose watch. It is defined in
[compose.dev.yaml](https://github.com/tripl-io/tripl/blob/main/compose.dev.yaml)
(this is **not** the deploy stack — production runs the published single-
container image via `compose.yaml`).

```bash
cp .env.example .env
docker compose -f compose.dev.yaml up --watch
```

Services started: `postgres` (pgvector, pg18), `rabbitmq`, `redis`, `api`,
`celery-worker`, `celery-beat`, and `frontend`. The dev worker runs two worker
processes inside its 1 GB limit; set `CELERY_WORKER_CONCURRENCY` for more.

| Surface | URL |
|---|---|
| Frontend (Vite dev server) | http://localhost:5173 |
| API | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |
| API health | http://localhost:8000/health |
| RabbitMQ management | http://localhost:15672 |

Useful facts about the dev stack:

- The `api` service runs `alembic upgrade head` before launching `uvicorn`, so
  the schema is migrated on startup. `celery-worker` and `celery-beat` wait for
  the `api` healthcheck for that reason: started earlier, they would boot
  against an unmigrated schema, where the settings-override load degrades to a
  warning and that worker silently ignores persisted overrides until restarted.
- `DEBUG=true` is set for the dev `api`, which relaxes the production-readiness
  checks (see [Common dev failures](#common-dev-failures)).
- `celery-beat` polls for due metrics every 5 minutes (the `check-metrics-due`
  task; scans themselves fire on interval boundaries) and also schedules the
  daily/weekly maintenance and digest jobs. The actual scan/metrics/alert work
  runs on `celery-worker`.
- Watch uses file polling inside containers (`WATCHFILES_FORCE_POLLING` on the
  backend, `CHOKIDAR_USEPOLLING` on the frontend) so edits under `backend/src`
  and `frontend/src` sync automatically. Changing a lockfile, `pyproject.toml`,
  or a `Dockerfile` triggers a rebuild.

You can validate Compose wiring without bringing the stack up:

```bash
docker compose -f compose.dev.yaml config
```

## Backend Workflow

The backend lives in [`backend/`](https://github.com/tripl-io/tripl/blob/main/backend).
Run these from inside that directory.

```bash
cd backend
uv sync --extra dev                           # install deps from uv.lock, incl. dev extras
uv run pytest                                 # full test suite
uv run pytest src/tripl/tests/test_events.py -v   # single test file
uv run pytest --cov=tripl --cov-report=term-missing:skip-covered   # with coverage (see "Coverage")
uv run ruff check                             # lint
uv run ruff format --check                    # formatting check (drop --check to apply)
uv run mypy                                   # strict type check
```

`--extra dev` is not optional: uv does not install optional-dependency extras by
default, so a bare `uv sync` gives you the app's runtime deps and none of the
tooling above. `make install` does this for you, and installs the git hooks.

### Formatting is enforced, not suggested

`ruff format` runs automatically on staged backend files through the git
pre-commit hook. Point git at the versioned hooks once per clone:

```bash
make install-hooks   # bd hooks install --beads
```

The hook rewrites the files in place and then fails the commit, so you can see
what changed and `git add` it — formatting is applied for you, but nothing is
committed behind your back. CI runs `ruff format --check src/` too, so a
`--no-verify` commit still gets caught at the PR.

The hook itself lives in [`.beads/hooks/pre-commit`](.beads/hooks/pre-commit),
below beads' own section markers (beads preserves anything outside them). It is a
versioned file, so `make install-hooks` is the only setup step — it just points
`core.hooksPath` at `.beads/hooks`.

That path is stored in `.git/config` as an absolute, machine-specific value, so a
clone or a moved working copy can end up pointing at a directory that no longer
exists. When that happens **no hook runs at all** — beads' own sync included, and
silently. `bd hooks list` tells you; `make install-hooks` repairs it.

Notes:

- Tests run against an in-memory SQLite database (`aiosqlite`), so `uv run
  pytest` needs **no** Postgres, RabbitMQ, or warehouse running. `pytest-asyncio`
  is in `auto` mode and the loop scope is session-wide (see
  `[tool.pytest.ini_options]` in `backend/pyproject.toml`).
- The schema is built once per test process and every row is deleted after each
  test (see `setup_db` in `src/tripl/tests/conftest.py`); a test that alters the
  shared schema itself triggers a full rebuild. The conftest also lowers the
  scrypt cost for the suite, so password hashing does not dominate run time.
- CI runs the suite in parallel with `pytest-xdist` (`uv run pytest -n auto
  --dist worksteal ...`). Each worker has its own in-memory database, so
  `-n auto` works locally too; on a small machine prefer `-n 2` or plain
  `uv run pytest`.
- Ruff is configured for `target-version = py314`, `line-length = 100`, rule set
  `E, F, I, UP, B, SIM`, and excludes generated migrations under
  `alembic/versions`.
- mypy runs in `strict` mode over `src/tripl` and excludes the test tree.
- Run the checks for the side you touched before opening a PR.

### Alert digest concurrency gate (needs a real PostgreSQL)

`flush_due_alert_digests` is kept safe under concurrency by two things the rest
of the suite cannot execute, because it runs on SQLite: the plain
`SELECT ... FOR UPDATE` in `_build_digest`, which SQLite does not emit at all,
and `_try_acquire_advisory_lock`, which returns "acquired" off PostgreSQL
without asking the database. `src/tripl/tests/test_alert_digest_concurrency_pg.py`
runs both against a real server. Without one it SKIPS, so a plain
`uv run pytest` is unaffected; CI runs it as its own job with
`TRIPL_TEST_PG_REQUIRED=1`, which turns that skip into a failure.

```bash
docker run --rm -d --name tripl-digest-pg -p 55432:5432 \
  -e POSTGRES_USER=tripl -e POSTGRES_PASSWORD=tripl -e POSTGRES_DB=tripl_digest \
  postgres:18

cd backend
TRIPL_TEST_PG_URL=postgresql+psycopg://tripl:tripl@localhost:55432/tripl_digest \
  uv run pytest -q -m "pg_concurrency or postgres"
```

`postgres` marks the other tests that need this database: today the
scan-identity repair migration run through asyncpg, the driver production
uses — its stamping UPDATE was valid SQLite and invalid prepared PostgreSQL,
and only a real server could say so.

Stock `postgres` is enough here, unlike the relevance and migration jobs: the
gate builds its schema from `Base.metadata.create_all` rather than running the
revision chain, so nothing asks for the `vector` extension. It drops and
recreates every table per test, which is why the database name is
`tripl_digest` and never `tripl`.

The `FOR UPDATE` case is the one worth understanding. It holds a buffered row's
lock — standing in for a `collect_metrics` that has written the row and not yet
committed — and asserts the flush **blocks** rather than returning. With
`SKIP LOCKED` it would sail past and ship a digest silently missing exactly the
scope that is firing hardest. Flip `.with_for_update()` to
`.with_for_update(skip_locked=True)` and the test fails on that assertion,
which is what makes it a gate rather than decoration.

### Search relevance harness (needs a real PostgreSQL)

Because the suite runs on SQLite, search keeps a Python fallback and the
**production ranking SQL** — `ts_rank_cd`, the trigram/boost tiers,
`merge_results`, and the `tripl_search` text-search configuration — is executed by
nothing else in the repo. `src/tripl/tests/relevance/` ranks a fixed, readable
corpus with that real SQL on a real PostgreSQL. Without a server it
SKIPS, so a plain `uv run pytest` is unaffected; CI runs it as its own job with
`TRIPL_RELEVANCE_REQUIRED=1`, which turns that skip into a failure.

```bash
docker run --rm -d --name tripl-relevance -p 55442:5432 \
  -e POSTGRES_USER=tripl -e POSTGRES_PASSWORD=tripl -e POSTGRES_DB=tripl_relevance \
  pgvector/pgvector:0.8.7-pg18-trixie

cd backend
TRIPL_RELEVANCE_PG_PORT=55442 \
  uv run pytest -q -m relevance src/tripl/tests/relevance
```

The pgvector image is required, not stock postgres: the harness migrates the
database to head with the real revision chain (so it tests the shipped
`tripl_search` configuration, not a hand-rolled copy of it), and one revision
creates the `vector` extension. It drops and recreates schema `public` on every
run, which is why the database name defaults to `tripl_relevance` and never
`tripl`.

Every case in `relevance/cases.py` passes, alongside a self-test that proves the
harness really is on PostgreSQL with the semantic leg off, and
`relevance/test_semantic_floor.py`, which covers the semantic leg's cosine floor
and confidence using hand-built vectors (no provider, no API key). Read the count
off the file rather than from here — this sentence has already been wrong once.

`RelevanceCase` carries an `xfail_ordering` field and `test_search_relevance.py`
turns it into `xfail(strict=True)`, so the workflow for a measured fault is:
write the case down with the marker first, fix it second, delete the marker as
the proof. **That has now happened once, end to end** —
`russian-phrase-finds-the-event-it-describes` was written with the marker, and
The coverage-term fix deleted it. Strict is what makes the last
step honest: an xfail that starts passing FAILS, so a marker cannot outlive the
fault it describes.

Only ORDERING may ever be xfailed, never retrieval — a ranking nuance must not
excuse a document vanishing from the results, which is why the two are separate
tests and only one of them reads the field.

Do not weaken an assertion to make a case pass.

**Which guarantees are PostgreSQL-only.** The rest of the backend suite runs on
in-memory SQLite against `_search_query.fallback_score`, a tier ladder with no
`ts_rank_cd`, no trigram similarity and **no stemmer**. So the SQLite suite does
**not** cover ranking: everything the stemming fix covered (stemming, and the 3.25 boost
tier that depends on it — the `purchases` / `уловы` / `spots` / `экран спота`
cases) holds only on PostgreSQL and only this job executes it. What does hold on
both dialects is anything implemented at document-build time (the
keyword change, the spaced aliases and the query fold) and the rule that
only an exact title/keywords match may be reported at confidence 1.0. The
`fallback_score` docstring carries the same list next to the code.

## Frontend Workflow

The frontend lives in [`frontend/`](https://github.com/tripl-io/tripl/blob/main/frontend)
(React 19 + TypeScript + Vite, Tailwind 4, Radix UI, TanStack Query, Recharts).

```bash
cd frontend
bun install                # install deps from bun.lock
bun run dev                # Vite dev server on :5173
bun run test               # vitest run
bun run test:coverage      # the same run with v8 coverage and its thresholds (see "Coverage")
bun run lint               # oxlint plus the project rule tests  (zero-warning policy)
bun run build              # tsc -b && vite build  (full type check with TypeScript 7 + production build)
bun run check:bundle       # after a build: first-load JavaScript stays inside its budget
```

How the test suite is set up (`vite.config.ts`, `src/test-setup.ts`):

- `*.test.ts` files run in the `node` environment and `*.test.tsx` files in
  `jsdom`. A `.ts` test that needs a DOM (a hook tested through `renderHook`)
  starts with `// @vitest-environment jsdom`. Run one side with
  `bunx --bun vitest run --project node` (or `--project jsdom`).
- A test fails if it prints through `console.error` or `console.warn`: that is
  how React reports invalid DOM nesting and updates outside `act()`, and how
  react-query reports a query that resolved to `undefined` (usually a bare
  `vi.fn()` mock). Fix the cause, or spy on `console` yourself when the message
  is what the test is about. Known, tracked noise is listed in
  `KNOWN_CONSOLE_NOISE` with the issue that removes it.
- Storage, timers, stubbed globals and `vi.spyOn` spies are reset after every
  test, and `window.matchMedia` answers "no match" unless a test installs its own.
- `src/test/axe.ts` runs axe over a rendered tree; `src/test/storage.ts` makes
  storage throw the way private mode does.

### End-to-end tests (Playwright)

`frontend/e2e/` drives a real browser against a running stack: the whole
pipeline a visitor drives, worker and synthetic warehouse included. The tests
do not start the stack; bring up the dev stack first, with sign-up rate limits
off so every test can sign up an account of its own:

```bash
RATE_LIMIT_ENABLED=false docker compose -f compose.dev.yaml up --build -d api celery-worker celery-beat frontend
cd frontend
bunx playwright install chromium   # once
bun run test:e2e                   # against http://127.0.0.1:5173
```

`e2e/critical-flows.spec.ts` is the smoke suite: sign-up and sign-in through
the forms, creating a project, a scan run by the worker, and an anomaly in the
alert inbox — the path a new team walks on day one, and the one every other
spec assumes.

**A new feature ships with an end-to-end test.** A pull request that adds
user-facing functionality — a page, a card, a flow, a new kind of object a
person creates or acts on — adds a spec under `frontend/e2e/` that walks it in
the browser: the happy path from where a person finds it to the result they
see, including the write and its undo where there is one. Unit and API tests
still cover the edge cases; the end-to-end test proves the pieces meet. Specs
build on the shared fixtures (`fixtures.ts`): a fresh `account` per test, and
`generateDemo` / `deleteDemo` when the feature is easiest to reach on the demo
workspace, and `signInAsOwner` for organization settings: global setup
(`e2e/global-setup.ts`) signs up the stack's first account, its owner, before
any test runs. Against a stack that already has an owner, pass one in with
`E2E_OWNER_EMAIL` and `E2E_OWNER_PASSWORD`. A fix or refactor of an existing flow extends that flow's spec when
the change is visible in it. Say in the pull request which spec covers the
feature, or why none can.

`E2E_BASE_URL` points the tests at another instance. `E2E_CHROMIUM` uses an
installed Chromium (`E2E_CHROMIUM=/usr/bin/chromium`) where Playwright ships no
browser, an arm64 Linux host for one. A failure leaves a trace, screenshot and
video under `frontend/test-results/` (`bunx playwright show-trace <zip>`).
CI runs the same tests in the `E2E` job.

Layout is asserted here, not in unit tests: jsdom lays nothing out, so a unit
test can only check class names, which a refactor breaks and a real regression
passes. `e2e/demo-layout.spec.ts` measures boxes at 1440 and 375 px and
compares screenshots (`toHaveScreenshot`) of the demo bar, the tour and the
coach card in light and dark. The baselines under
`e2e/<spec>.ts-snapshots/` are Linux Chromium's and come from CI: a missing or
changed one fails the `E2E` job (the report's diff shows what moved). Once the
change is intended, put the `update-snapshots` label on the pull request and
push: that run writes the baselines instead of failing and uploads them as the
`e2e-snapshots` artifact, to commit under `frontend/e2e/`. Take the label off
before merging.

The typed API client is generated from the backend's OpenAPI schema. If you
change request/response contracts, regenerate it:

```bash
bun run gen:api     # regenerates src/types/api.gen.ts from ../backend/openapi.json
```

`bun run lint` enforces a zero-warning policy and `bun run build` runs a full
type-check, so both must be clean before you push frontend changes.

### Linting

`bun run lint` runs [Oxlint](https://oxc.rs/docs/guide/usage/linter)
(`oxlint --deny-warnings --report-unused-disable-directives`, about a second)
and then the tests of the project's own lint rules
(`node --test oxlint-plugins/*.test.js`). `.oxlintrc.json` is the whole rule
set; edit it directly. Oxlint also reports unused disable comments, which
still use the `eslint-disable` spelling.

One rule of the old ESLint set has no Oxlint counterpart and was dropped on
purpose: `no-octal`. A legacy octal literal (`017`) is already a syntax error
in ES modules and strict TypeScript, so the rule never had anything left to
catch.

The rules no stock plugin has live in a local JS plugin,
`oxlint-plugins/tripl.js`, loaded through `"jsPlugins"` (an alpha Oxlint
feature, so pin the `oxlint` version and re-run the rule tests on every bump):

- `tripl/no-bare-lazy`: code-split components go through `lazyWithReload`,
  not `React.lazy` (off in `src/lib/lazyWithReload.ts` itself).
- `tripl/no-query-key-literals`: query keys come from the builders in
  `src/lib/queryKeys.ts`, not an array literal (off in that file and in tests).
- `tripl/no-arbitrary-sizes`: text, icon and radius sizes use the named
  scales, not `text-[Npx]`, `size-[Npx]` or `rounded-[Npx]` (off in tests).
- `tripl/no-raw-select`: pages use `NativeSelect`, not a raw `<select>`
  (only `src/pages/**/*.tsx`; the two files in the override that turns it off
  say why beside their line: the event form's own select primitive and the
  audit log's filter-bar chip).
- `tripl/no-muted-foreground`: no `muted-foreground` class or `var()` (off in
  tests). The alias is gone from `index.css`; use `text-fg-tertiary` for
  captions and meta, `text-fg-secondary` for body copy.

Which files each rule covers is set in the `overrides` of `.oxlintrc.json`: a
later override wins over an earlier one for the files both match. A new rule
goes into `tripl.js` with valid and invalid cases in `tripl.test.js`, which
runs them through Oxlint's `RuleTester`.

### TypeScript 6 and 7 side by side

Type checking uses TypeScript 7 (the native compiler, `tsc -b` in about 4 s
instead of about 50 s). `openapi-typescript` (`bun run gen:api`) loads the
TypeScript API and still needs TypeScript 6, which has no successor API until
7.1. `package.json` therefore installs both, as the TypeScript 7.0 release
notes describe:
`"typescript": "npm:@typescript/typescript6@…"` keeps `import 'typescript'`
on TypeScript 6 (its command is `tsc6`), and
`"@typescript/native": "npm:typescript@^7…"` provides `tsc`. So
`bunx tsc` is TypeScript 7 and `bunx tsc6` is TypeScript 6.

## Coverage

CI measures coverage on both sides and fails when it drops below a recorded
floor. **No floor is recorded yet:** every threshold is `0` until a baseline
is measured on CI (the `Pytest` job log and the `frontend-coverage-summary`
artifact) and written in by a maintainer, so today the numbers are reported but
nothing fails on them.

```bash
# backend, from backend/
uv run pytest -m "not conformance and not relevance and not pg_concurrency" \
  --cov=tripl --cov-report=term-missing:skip-covered
# frontend, from frontend/
bun run test:coverage
```

- **Backend:** pytest-cov over the `tripl` package, test modules and
  migrations omitted. The floor is `fail_under` in `[tool.coverage.report]`
  of `backend/pyproject.toml`; pytest-cov applies it whenever `--cov` is on, so
  CI passes no `--cov-fail-under` of its own. Use the same `-m` expression as
  CI when you compare numbers: a local run that also executes (or skips) the
  PostgreSQL lanes measures a different set.
- **Frontend:** `@vitest/coverage-v8`, configured under `test.coverage` in
  `frontend/vite.config.ts`. Every file under `src/` counts, whether a test
  imports it or not; tests, `src/test/`, the generated `src/types/api.gen.ts`
  and `src/main.tsx` are excluded. Besides the app-wide `thresholds`,
  `src/lib/**` and `src/demo/**` carry their own, so a drop there cannot hide
  behind the average. CI uploads `coverage/coverage-summary.json` as the
  `frontend-coverage-summary` artifact, from red runs too.

The floors are a ratchet, starting from that recorded baseline — a PR does not
set the first one. Once it is in, when your change raises coverage, raise the matching
threshold to the new measured value (rounded down) in the same PR. Never lower
one to get a red run through: write the missing test instead, or, if code with
its tests really was deleted, say so in the PR description where the reviewer
can check it.

## Database Migrations (Alembic)

Migrations live in `backend/alembic/`. The async `env.py` wires Alembic to
`tripl.config.settings.database_url` and `tripl.models.Base.metadata`, so a
migration run needs a reachable Postgres (`DATABASE_URL`) and the models
importable.

Typical dev loop after changing SQLAlchemy models:

```bash
cd backend
uv run alembic revision --autogenerate -m "describe the change"   # generate
# review the generated file under alembic/versions/, then:
uv run alembic upgrade head                                       # apply
```

:::tip alembic shebang fallback
If `uv run alembic` fails with a broken shebang (e.g. after a directory rename
left `.venv/bin/alembic` pointing at a stale path), call the module directly:

```bash
uv run python -m alembic revision --autogenerate -m "msg"
uv run python -m alembic upgrade head
```
:::

Always review autogenerated migrations — Alembic does not catch everything
(e.g. enum changes, server defaults, data backfills). The suite includes
`test_alembic_revisions.py`, which guards migration integrity, so run
`uv run pytest` after adding a revision.

## Architecture: where new code belongs

tripl is one codebase with two runtimes sharing a common core.

### Shared core kernel

Provider-agnostic, framework-agnostic logic lives in
[`backend/src/tripl/core/`](https://github.com/tripl-io/tripl/blob/main/backend/src/tripl/core):

- `core/adapters/` — warehouse connectors (`clickhouse.py`, `bigquery.py`,
  `databricks.py`, `postgres.py`) behind a shared `base.py` interface and a `registry.py`.
- `core/analyzers/` — scan and quality logic: cardinality analysis, event/
  variable generation, anomaly detection, distribution drift, release
  regression, and preview.
- `core/intervals.py` — shared time-bucket helpers.

The core kernel exists so both the API and the worker call the **same** scan,
metrics, and anomaly logic. Put analytics logic here (not in a router or a
Celery task) so it stays reusable and unit-testable in isolation. Add a new
warehouse by implementing the adapter `base` interface and registering it in
`core/adapters/registry.py` — extend the registry rather than branching on
warehouse type elsewhere.

### API runtime (async) vs worker runtime (sync)

- **API** (`api/`, `services/`, `models/`, `schemas/`, `main.py`) runs on
  FastAPI with **async** SQLAlchemy + `asyncpg` via `DATABASE_URL`. Keep request
  paths async — do not introduce blocking DB calls into a router.
- **Worker** (`worker/`) runs Celery tasks using **sync** SQLAlchemy + `psycopg`
  via `SYNC_DATABASE_URL`. Sync DB access is expected and acceptable inside
  worker tasks.

When adding an HTTP feature:

1. Add a **thin** router in `api/v1/<area>.py` — parse/validate, call a service,
   return a schema. No business rules here.
2. Put business logic in `services/<area>_service.py`.
3. Add SQLAlchemy models in `models/` and Pydantic request/response models in
   `schemas/`. Update both together when a payload changes, and regenerate the
   frontend types (`bun run gen:api`) so `frontend/src/types` stays in sync.

When adding heavy, retryable, or scheduled work:

1. Add a Celery entrypoint under `worker/tasks/` (scan, metrics, alerts,
   maintenance, search).
2. Put the actual analysis in `core/analyzers/` and any warehouse access in a
   `core/adapters/` adapter.
3. Prefer extending an existing task/adapter/analyzer flow over adding a
   parallel implementation.

Operational invariants to preserve unless you are intentionally changing them:
RabbitMQ is the Celery broker; PostgreSQL is the system of record for catalog,
metrics, anomalies, and alert deliveries; warehouses are read-only external data
sources; and API, worker, and beat must all stay runnable together via Compose.

## Common dev failures

- **Scan / connection test fails with no warehouse reachable.** Warehouses are
  external and not started by Compose. Point a data source at a real ClickHouse,
  BigQuery, Databricks, Snowflake, Trino, or Postgres warehouse you control. (A
  Trino coordinator runs in one container: `docker run -d -p 8080:8080 trinodb/trino`,
  then a `trino` source on port 8080 with scheme `http` and catalog `tpch`.)
- **App refuses to start in non-debug mode.** `Settings.assert_production_ready()`
  rejects an empty/invalid `ENCRYPTION_KEY`, an empty `SECRET_KEY`, CORS that
  resolves to nothing or to the wildcard `*`, or `SESSION_COOKIE_SECURE=false`
  (it also requires the database/broker URLs). The dev stack sets `DEBUG=true`
  so these are tolerated locally; if you run the API outside dev mode you must
  supply real values. Generation commands are documented inline in
  [`.env.example`](https://github.com/tripl-io/tripl/blob/main/.env.example).
- **`alembic` shebang errors.** Use `uv run python -m alembic ...` (see the
  migrations section).
- **Lockfile drift / CI mismatch.** Always use `uv` and `bun`. A stray `pip`,
  `npm`, or `yarn` install will desync the lockfiles.
- **Port already in use.** The dev stack binds `5173`, `8000`, `5432`, `5672`,
  `6379`, and `15672`. Stop conflicting services or remap ports.
- **Edits not hot-reloading.** Compose watch only syncs `backend/src`,
  `frontend/src`, and a few config files; changes to `pyproject.toml`,
  lockfiles, or a `Dockerfile` require a rebuild (re-run `up --watch`).

## Licensing of contributions

Tripl is maintained by one copyright holder:

- the server and the web app are licensed under AGPL-3.0-or-later;
- the CLI and the MCP server are licensed under Apache-2.0.

To keep the project free to choose its licenses later, commercial licenses
included, every contributor signs the [Contributor License Agreement](CLA.md)
once. You keep the copyright in your work; the agreement grants the project a
license to use it.

The `CLA` check comments on your first pull request. Reply with the sentence
it quotes to sign. Signatures are recorded on the `cla-signatures` branch.
Bots and the maintainer are exempt.

A pull request cannot be merged until the check passes. Contributing on behalf
of a company? Ask for the corporate agreement first; see [CLA.md](CLA.md).

## Pull Request Conventions

- **Title format:** `[analytics] <Title>`.
- Keep PRs focused and run the checks for the side(s) you touched: backend
  (`pytest`, `ruff check`, `ruff format --check`, `mypy`) and/or frontend
  (`bun run lint`, `bun run test`, `bun run build`). Run `docker compose -f
  compose.dev.yaml config` when you change Compose or env wiring.

Always call out in the PR description when a change touches:

- **API contracts** — request/response shapes (and regenerate `bun run gen:api`).
- **Event/tracking-plan schema** — models or Pydantic schemas.
- **Queue, task, or schedule** behavior — Celery tasks or the beat schedule.
- **Metrics or anomaly semantics** — collection, bucketing, or detection logic.
- **Alerting** — channels, templates, or delivery behavior.
- **Environment variables** — and keep `.env.example`, the Compose env blocks,
  and `backend/src/tripl/config.py` synchronized.
- **A screen the docs show** — the docs site's screenshots are taken by a
  script; retake the ones your change affects
  ([website/screenshots/README.md](website/screenshots/README.md)).

For deeper area-by-area pointers (which service, schema, task, and test files
correspond to each feature), see
[AGENTS.md](https://github.com/tripl-io/tripl/blob/main/AGENTS.md).
