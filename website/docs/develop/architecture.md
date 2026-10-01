# Architecture

The technical picture of how tripl is built. If you want the *what* and the
*why* in plain language first, read **[concepts.md](../use/concepts.md)**; this page is
the *how* for people working on the system.

For local setup, commands, and the source tree, see
**[CONTRIBUTING.md](https://github.com/vladenisov/tripl/blob/main/CONTRIBUTING.md)**.

---

## System shape

tripl is three cooperating processes plus a database and a message broker:

```
                      ┌─────────────┐
   browser  ───────▶  │  frontend   │  React + Vite (static SPA)
                      └──────┬──────┘
                             │ HTTP /api/v1
                      ┌──────▼──────┐        ┌──────────────┐
                      │     api     │◀──────▶│  PostgreSQL  │  system of record
                      │  (FastAPI)  │        └──────▲───────┘
                      └──────┬──────┘               │
                             │ enqueue              │ read/write
                      ┌──────▼──────┐        ┌──────┴───────┐
                      │  RabbitMQ   │        │   workers    │
                      │  (broker)   │◀──────▶│   (Celery)   │
                      └─────────────┘        └──────┬───────┘
                                                    │ read-only queries
                                             ┌──────▼───────┐
                                             │  warehouses  │  ClickHouse /
                                             │  (external)  │  BigQuery / Postgres
                                             └──────────────┘
```

- **api** — FastAPI service. Owns all HTTP, auth, and business logic; reads and
  writes PostgreSQL; enqueues background work onto RabbitMQ.
- **celery-worker** — runs scans, collects metrics, detects anomalies and drift,
  and dispatches alerts. It is the only process that connects to the external
  warehouses.
- **celery-beat** — the scheduler. Triggers due metric-collection checks — for
  both event counts and the **metric catalog** (a ~300 s due-check) — and the
  schema, distribution-drift, and scan-job retention cleanup. It also polls implementation tickets, runs the daily lifecycle (sunset) watch, chases
  stranded search embeddings, reaps stuck alert deliveries (retrying
  transiently-failed ones for a bounded window), and runs periodic
  alert/maintenance tasks.
- **PostgreSQL** — the system of record for the plan, metrics, anomalies, audit
  log, and alert deliveries.
- **RabbitMQ** — the broker between the api and the workers.
- The **warehouses are external** and are never started by tripl; the worker
  only ever issues read queries against them.

Locally, all of the above (except the warehouses) run under Docker Compose:
`postgres`, `rabbitmq`, `api`, `celery-worker`, `celery-beat`, and `frontend`.

---

## Backend (`backend/`)

- **FastAPI** with fully async request paths.
- **SQLAlchemy** (async) over **PostgreSQL**, migrations via **Alembic**.
- **Pydantic v2** schemas as the request/response contract.
- Routers live under `src/tripl/api/v1` and stay thin; business rules live in
  `src/tripl/services`.
- Shared compute that both the request path and the worker need — warehouse
  **adapters**, **analyzers** (anomaly/drift/scan logic), and interval helpers —
  lives in a neutral `src/tripl/core` kernel that imports neither `services` nor
  `worker`. This keeps `services` from importing `worker` at module level; the
  request path reaches the worker only via lazy, runtime Celery dispatch.
- DB engine and pool configuration is centralized in `src/tripl/db_config.py`
  (an async pooled engine for the API, a sync pooled engine for Celery; the
  worker→async bridge uses a throwaway NullPool engine — see
  `worker/search_reindex.py`). PostgreSQL connections pin the session timezone
  to UTC for both asyncpg and psycopg.
- Migrations are applied by the Compose `migrate` one-shot via `alembic upgrade
  head` before the API and workers start. Alembic uses the asyncpg
  `DATABASE_URL`; the worker uses the psycopg `SYNC_DATABASE_URL`. Reserved
  characters in URL credentials are percent-encoded. The app process does not
  run migrations on startup; its lifespan configures logging and asserts
  production readiness.
- **Migrations are executed in CI, not just parsed.** The `migrations` job stands
  up the same `pgvector/pgvector` image the Compose stack uses (the baseline enables
  `pg_trgm`, `unaccent` and `vector`, so a stock `postgres` image cannot run it)
  and does a full round trip on an empty database: `upgrade head`, then
  `downgrade base`, then `upgrade head` again, asserting after each leg that
  Alembic is where it claims and that the downgrade left no table or enum type
  behind. It runs on every push to `main`, on the release gate, and on pull
  requests that touch `backend/alembic/`. A downgrade you cannot implement must
  be a documented no-op, never a `raise` — a raising downgrade would make the
  round trip unrunnable.
- Health check: `GET /health`.

### Authentication & access

- **Session auth** via an HTTP-only cookie for interactive users. Emails are
  validated with `email-validator` (RFC 5321 / 6531).
- **API keys** (Bearer tokens, `Authorization: Bearer tk_…`) for scripts and
  agents. Only the SHA-256 hash of a key is stored. Keys carry a `read` or
  `write` scope, an optional project binding, and an optional expiry, and are
  revocable. See **[agent-api-guide.md](../integrate/agent-api-guide.md)**.
- **RBAC** in two layers plus an operator flag. The organization role
  (`organization_members.role`: owner / admin / member) and the project role
  (`project_members.role`: editor / viewer) decide everything inside an
  organization; `services/project_access.py` turns them into one project role
  (org owner or admin of the project's own organization = `owner` in every
  project of it; a member = their row; no row = `404`). `users.is_platform_admin`
  grants the operator settings and nothing else. There is no instance-wide role
  (the old `users.role` is dropped; a guard test keeps it out). Owner-gated
  routes (`deps.get_owner_user`) take an
  org owner or admin in an interactive session and are not reachable with an
  API key. One enumerated exception carries a separate gate
  (`deps.get_key_reachable_owner_user`): the
  [metrics replay](../integrate/agent-api-guide.md#replaying-metrics) accepts an
  org owner's or admin's `write`-scoped key, and a test pins the route list so
  it stays one. `/settings` takes `deps.get_settings_admin_user` (a platform
  admin, or owner/admin of the default organization), and a write touching an
  operator field additionally needs `deps.require_platform_admin`.

---

## Worker (`backend/src/tripl/worker/`)

- **Celery** app with a **RabbitMQ** broker.
- **Warehouse adapters** (`core/adapters`) provide a common interface over
  **ClickHouse**, **BigQuery**, and **PostgreSQL** source databases. A common
  interface is not the same as identical behavior, and it is emphatically not the
  same as an equally *verified* behavior:
  - **ClickHouse and PostgreSQL are executed** in CI. The `conformance` job stands
    up real `clickhouse-server` and `postgres` containers, runs the SQL the
    adapters generate, and compares the results against the reference
    implementation. Their bucket values, counts and contract counts are proven.
  - **BigQuery is analyzed on every PR and has a real value suite.** CI
    posts every generated statement to an emulator embedding Google's real
    **ZetaSQL analyzer**, which is authoritative on valid GoogleSQL. A separate
    credentialed suite has executed a typed, table-less nine-row fixture on real
    BigQuery and compared bucket values, counts, aggregates, breakdowns, nested
    JSON/STRUCT values and field-contract counts with the shared reference. Its
    trusted-release workflow reruns the suite for each `vX.Y.Z` release tag once
    credentials and the explicit enable flag are configured. The same release gate
    also drives scan, replay, catalog metrics, batched collection, and anomaly
    recalculation against real BigQuery while keeping PostgreSQL as the application
    database. Pull requests retain the credential-free analyzer gate.

  The gates live in `backend/src/tripl/tests/conformance/`. See the
  **[warehouse capability matrix](warehouse-parity.md)** for the per-capability
  proven/believed/bounded breakdown, which paths are still sampled or depth-capped,
  the supported time types per dialect, and the UTC / Monday-week bucket contract
  every adapter must honor.
- **Dialect awareness** (`core/adapters/measure_validator`) centralizes identifier
  quoting, string/number/timestamp literals and a pre-flight `lint_dialect_sql`
  check per `SqlDialect`, so a query that provably cannot resolve on the selected
  warehouse is rejected at preview and save time rather than inside a worker. The lint runs
  *after* the read-only gate and can only reject more, never admit more.
- **Analyzers** (`core/analyzers`) hold the scan, anomaly, and drift logic.
  (Both live in the shared `core` kernel — see the Backend section — so the
  request path can reuse them without importing the worker package.)
- **Tasks** (`worker/tasks`) are the Celery entrypoints for scans, metrics,
  anomalies, and alert delivery.

### Detection

- **Anomaly detection** runs at three scopes — project-total, event-type, and
  event. It combines **z-score** thresholds with **seasonality** decomposition
  (**STL / MSTL**) so it understands daily and weekly rhythms rather than just a
  flat baseline.
- **Forecast** — a next-bucket extrapolation, rendered on the metric chart as a
  hollow point with a whisker for its likely range.
- **Schema drift** — detects fields appearing, disappearing, or carrying new
  values; keeps sample values; and prunes old drift records on a retention
  schedule.
- **Variable value drift** — compares scan-observed values with a variable's
  effective documented list (per-event override, otherwise global) and keeps
  the review state independent from later evidence refreshes. Accepted rows are
  frozen: their stored values are the accepted set, and a scan reopens the row
  only for values outside it.
- **Property drift** (F23) — compares each event's property list with what a
  scan saw (new property, missing required property, per-property type
  change). Its open rows are counted by one implementation
  (`services/_open_signals.py`) for the project summary and the health score,
  and become `property_drift` alert candidates through one mapping
  (`alerting_property_drift.py`) shared by dispatch and the replay.
- **Distribution drift** — uses **PSI** (Population Stability Index) over event
  field values.
- **Release regression** — activation-gated comparison of the newest stable app
  version with the previous release, inert unless a scan names an app-version
  column.
- **App-version series retention** is a project-level read-time policy. Scans
  and catalog metrics select their source column, all raw version buckets stay
  stored verbatim, and `Project.app_version_keep_releases` decides which latest
  releases remain explicit versus fold into `Other`. Changing it therefore
  affects event and catalog-metric views immediately without replaying data.
- **Correlation-aware grouping** collapses signals that share an underlying
  cause so one root problem yields one alert, not many.
- **Metric anomalies** run the same detector at a dedicated **metric scope**.
  Metrics are classified **count-shaped** (counts/sums) or **fractional** (ratios,
  averages, raw SQL): count-shaped series keep zero-fill and the
  `min_expected_count` gate, while fractional series drop the zero-fill (a
  missing bucket means "no data", not zero) and read the gate against the
  *magnitude* of the expectation, so sub-unit ratios don't false-fire and a level
  that legitimately sits below zero is not rejected for its sign. Per-project
  `detect_metrics` enables the scope; per-rule `include_metrics` opts metric
  anomalies into alerting (off by default).

### Metrics

- Counts are collected into PostgreSQL on a configurable interval
  (15m / 1h / 6h / 1d / 1w), with **replay-by-chunk** support for backfills.
- Bulk metric upserts are chunked to stay under PostgreSQL's 65535 bind-parameter
  limit.

### Catalog metrics

- **`MetricDefinition`** is a user-defined, **project-scoped** metric (the
  catalog) — global rather than branched. Three kinds: **`sql`** (a user
  read-only `SELECT` or top-level `WITH ... SELECT` returning a per-bucket value
  against a data source on its own interval), **`fact`** (`count` /
  `sum` / `avg` / `min` / `max` / `count_distinct` over a measure column of a
  reusable fact table, with optional filters and breakdowns), and
  **`event_composition`** (a `single` event count, a `ratio` A/B, or an event
  `per_distinct_user`, derived from already-collected `event_metrics`).
- **Scheduling.** The `check_metric_definitions_due` beat task runs about every
  **300 s** and dispatches each **active** metric whose interval is due. SQL
  metrics use `collect_metric_definitions`; fact metrics are grouped by interval
  and use `collect_fact_metrics_batch`, which folds compatible aggregates into
  one multi-aggregate warehouse query per fact table. A manual collect on one
  fact metric discovers the other active metrics that reference either of its
  operands and sends the same dependency set through that batch path. Metrics on
  different interval grids remain separate groups. `event_composition` metrics
  derive from event series already collected on the shared scan grid: `single`
  and `ratio` need no warehouse query, while `per_distinct_user` additionally
  issues one bucketed `count(DISTINCT user_id)` against the source scan's data
  source for its denominator. Each run composes each grid in at most **two
  bounded regions**, never over the grid's full retained history:
  - the **resume region** — the two buckets before the metric's own last stored
    bucket on that grid, plus everything newer. A grid the metric has never
    stored a value for has no such anchor and is capped instead to
    `EVENT_COMPOSITION_BACKFILL_BUCKETS` (5,000) of that grid's intervals back
    from the head of the source series, so a metric that can never compose a
    value (a `per_distinct_user` whose denominator is always zero, say) cannot
    grow an unbounded query;
  - one **backfill chunk** — up to the same 5,000 intervals below the metric's
    own *oldest* stored bucket, clamped to the oldest bucket the source series
    actually has and to the resume floor so the two regions cannot overlap. The
    resume region alone is a one-way ratchet on `max(bucket)`, so without this a
    grid with more history than the first run's reach would be truncated at
    whatever that run happened to cover — and since a material definition edit
    clears every stored value, editing such a metric would destroy the part of
    its chart nothing could re-derive. Pre-history is instead filled in one
    bounded step per dispatch until the frontier meets the start of the series.

  What is still given up is the middle: buckets the metric has **already**
  composed, between its oldest stored bucket and the resume floor, are not
  revisited, so a historical event-metric bucket that changes after its composed
  value has scrolled out of the resume region is not recomputed. The backfill
  fills gaps; it does not repair a stored value whose source moved underneath
  it. One narrow stall: the frontier is the stored `min(bucket)` and a
  divide-by-zero bucket stores no row, so a chunk in which every bucket divides
  by zero leaves the frontier where it was and is retried on the next dispatch —
  one bounded pass wasted, not a permanent failure.

  A metric whose last collection **errored** is not retried before its own
  interval has elapsed (an hour for `event_composition`, which has no interval of
  its own): a failed run advances neither a value nor the completed-window
  watermark, so without that floor a metric that can never collect would be
  re-dispatched on every 300 s tick.
- **Aggregations.** Adapter `_aggregate_value_sql` builds the per-kind SQL for
  ClickHouse / BigQuery / PostgreSQL; `core/adapters/measure_validator` checks the
  measure/distinct column against the source's real columns before it reaches a
  query. Fact row filters persist in metric `config` as named `row_filters`,
  free-text `filter_sql`, and structured `conditions`; collection compiles them
  into one `AND` expression for both per-metric and batched aggregate paths.
- **Storage.** Values land in `metric_values`, with per-split rows in
  `metric_value_breakdowns` (platform / app-version / …, like event breakdowns).
  Each successful collection also advances a durable completed-window watermark,
  including when the source returns zero rows; due checks and the metric detail's
  next-update state therefore do not rescan an empty window every five minutes.
  A **divide-by-zero** in a `ratio` bucket produces **no value** — a gap, not a
  `0` — so the row is dropped rather than written as zero. Fact-ratio breakdowns
  are supported when numerator and denominator use the same fact table; each
  breakdown row stores that dimension value's numerator / denominator ratio, not
  a component that sums to the top-line ratio.
- **Surface.** Catalog CRUD lives at `/projects/{slug}/metrics`; a series read
  service feeds the frontend **MetricsPage** (list + kind-aware create/edit form)
  and the metric **drilldown**, which reuses the monitoring detail tabs. The
  drilldown also exposes schedule state and the non-executing
  `/{metric_id}/generated-sql` read endpoint. For fact metrics this endpoint
  expands the same active fact-table dependency closure as **Collect now** and
  returns the actual primary multi-aggregate statements grouped by fact table,
  interval, and replay chunk. Statement construction uses the same adapter
  builders as the worker without connecting to the warehouse. The fact-table
  column snapshot keeps both the normalized form type and the native warehouse
  type; this preserves BigQuery's distinct `TIMESTAMP`, `DATETIME`, and `DATE`
  bucket syntax. Older BigQuery fact tables must be previewed and saved once to
  capture that metadata. Breakdown scans are
  deliberately omitted and the response marks that explicitly. The diagnostic
  response is capped at 100 statements, 200 conditional aggregates per
  statement, 1,000,000 SQL characters, and 10,000 repeated metric-ID references.
  The compiler applies an input-size budget before assembling each statement;
  fact operands and fact tables accept at most 100 structured/named filters, and
  each free-text filter fragment is capped at 32,768 characters. A large
  replay/dependency graph therefore cannot turn this viewer-facing endpoint into
  an unbounded export.

---

## Frontend (`frontend/`)

- **React 19** + **TypeScript** + **Vite**.
- **Tailwind CSS 4** with **shadcn**-style UI primitives.
- **TanStack Query** for server state, **Recharts** for charts, **dnd-kit** for
  drag-and-drop reordering.
- The project information architecture is three job-based groups — **Plan /
  Observe / Govern** — defined once in `src/lib/navigation.ts` and consumed by
  both the sidebar and breadcrumbs. Data sources, members, API keys, personal
  security, and instance controls live in the separate Settings surface.
- **Serving.** In development the **Vite dev server** serves the SPA with HMR and
  proxies `/api` to the backend. In production there are two options: **(a)
  consolidated single container** — FastAPI serves the built SPA itself via
  `app.frontend()` (FastAPI 0.138+) when `SERVE_FRONTEND=true`, so one image
  serves API + SPA (root `Dockerfile` + the default `compose.yaml`, **no nginx**;
see [RELEASE.md](../run/release.md)); or
  **(b) standalone static tier** — `frontend/Dockerfile` serves the build through
  nginx (`frontend/nginx.conf`) next to the API. Consolidated mode routes the SPA
  through the API's `SecurityHeadersMiddleware`/`BrotliMiddleware`, so it inherits
  the same CSP/headers and compression; because the app is then the network edge,
  `rate_limit_trust_forwarded_for` stays `False` (don't trust client-sent
  forwarded headers) unless a trusted proxy is added in front.
- Plan branch context travels as a `?branch=` query parameter threaded through
  every plan API call and the React Query keys; the active branch is persisted
  in `localStorage` per project slug.

---

## Data model (core objects)

| Object | What it is |
|---|---|
| `Project` | A tracking-plan namespace — one product/world. |
| `EventType` | A folder grouping related events. |
| `Event` | A concrete tracked event. A scan-named one carries its scan identity in `source_name`, unique per event type (`uq_event_scan_identity`; a `NULL` identity is unconstrained). |
| `FieldDefinition` | A typed field on an event type. |
| `MetaFieldDefinition` | Project-level metadata carried by every event. |
| `Variable` | A typed `${placeholder}` with documented values, source bindings, and scan exclusion state. |
| `VariableValue` | One scan-observed variable context for an event/field. |
| `VariableEventValueOverride` | A complete per-event replacement for a variable's global documented list. |
| `VariableValueDrift` | Novel observed values plus their review/resolution state. |
| `Relation` | A declared connection between events. |
| `DataSource` | A connection to an external warehouse. |
| `ScanConfig` | A saved scan query + extraction rules. |
| `ScanJob` | One async execution of a scan config. |
| `EventMetric` | Time-bucketed counts for an event. |
| `MetricDefinition` | A user-defined metric (the metrics catalog); project-scoped, not branched. |
| `FactTable` | A reusable safe query, timestamp/column schema, and named filters for fact metrics. |
| `MetricValue` | Time-bucketed values for a `MetricDefinition`. |
| `MetricValueBreakdown` | Per-breakdown metric values (platform / app-version / …). |
| `MetricAnomaly` | A persisted anomaly bucket. |
| `AlertDestination` | A delivery channel (Slack, Telegram, …). |
| `AlertRule` | Filtering + delivery configuration for signals. |
| `AlertDelivery` | A record of one alert that was sent. |
| `ProjectTrackerConfig` | Owner-managed Jira or Linear settings (encrypted credential) for post-merge implementation tickets. |
| `LifecycleFinding` | Daily sunset-watch result per event and kind (`sunset_overdue`, `successor_silent`), upserted and resolved when the condition clears. |
| `ImplementationTicket` | A branch-merge ticket and the events it covers. |

Plan branches deep-copy the relevant objects (event types, fields, events,
variables, documented values/overrides/exclusions, meta fields, relations,
photos, comments) and merge back via a
3-way merge that preserves live IDs by natural key. Metrics are deliberately
**not** branched — they are project-scoped and shared across every branch.

### Organizations (in progress)

Organizations (F20, GH #273) ship in stages. The first stage adds the schema
and **changes no behaviour**:

- `organizations` and `organization_members` tables. The org roles are `owner`,
  `admin` and `member`. `users.is_platform_admin` marks the instance operator.
- A default organization with a fixed id (`DEFAULT_ORG_ID` in
  `models/organization.py`). The migration moves every existing row and user
  into it: owners become org owners, editors and viewers become members.
  Project memberships are left as they are. The backfill is a one-off
  snapshot: nothing writes org memberships, `is_platform_admin` or
  `invitations.org_role` yet, so the stage that starts reading them re-runs
  the idempotent `backfill_organizations()` in its own migration first.
- `organization_id` on `projects`, `data_sources`, `api_keys` and `invitations`
  (NOT NULL) and on `audit_log` (nullable, because platform actions have no
  organization). New rows get the default organization from both the ORM
  default and a server default. The server default keeps a container on the
  previous release able to insert during a deploy.
- `app_settings.organization_id`: NULL is the operator scope. Keys are unique
  per scope, and every settings read and write filters to the operator scope.

Later stages resolve the request's organization (`/api/v1/orgs/{org}/...`
paths, the API key's own organization) and key caches by project id. Since the
roles stage, permission checks read `organization_members` and
`project_members` (the old instance role, `users.role`, was dropped by a later
migration): the roles stage's migration re-ran the backfill, filled
`invitations.org_role`, and capped the project rows of former instance viewers
at `viewer`. Registration, invitation acceptance and `PATCH /users/{id}` write
organization roles; the first account of a self-hosted instance is the default
organization's owner and the platform admin. The owner-set advisory lock and
the last-owner rule are per organization. Per-org settings, per-org audit
filtering and multi-org sign-up come in later stages. The configuration page
lists the environment settings they will use.

---

## Operational flows

### Scan flow

1. The api creates or updates a `ScanConfig`.
2. Running it creates a `ScanJob`.
3. A Celery task executes the query against the warehouse via the adapter.
4. A **grouped** run resolves each distinct value of the event type column to an
   `EventType` through the shared resolver
   (`worker.utils.event_types.ensure_event_type_with_fields`), creating the type
   and a `FieldDefinition` per unreserved column when it is absent — the same
   call, on the same main-plan lookup, that Phase 1 of metrics collection makes.
   The reserved set it skips is the one the run already passes to
   `generate_events` as `reserved_columns`, so a column denied a field is the
   same column the generator stays quiet about. A run with a single configured
   event type resolves that one instead and creates nothing.
5. Cardinality analysis shapes each column: a low-cardinality scalar column is
   enumerated into event identities, a high-cardinality one collapses into a
   `${token}` template whose placeholders become variables. It does **not** gate
   variable creation on a JSON column — every discovered path that is not a
   declared passthrough (`json_value_paths`, the scan's *JSON values to keep
   as-is*) becomes a variable whatever its cardinality, which is why a JSON map
   keyed by user-typed text mints one variable per key. Bindings adopt existing
   variables and naming/group rules produce stable event identities.
6. Events and variables are created or updated in PostgreSQL. Scan writes do
   not overwrite user-authored field values or recreate excluded variables.
   One event per scan identity per event type is a unique key,
   `uq_event_scan_identity` on `(event_type_id, source_name)` — an event type
   lives on one branch of one project, so the two columns scope the identity
   per project, per branch, per type, and `NULL` stays free (tripl-8tdl). The
   API's `create_event` pre-check answers `409` naming the holder before the
   database would; a create that loses the concurrent INSERT race gets the same
   `409` body (`POST /projects/{slug}/events` and `/events/bulk`, the latter
   prefixed `Event N of M: `), while a scan that loses it adopts the holder
   inside a savepoint and carries on. Before the baseline squash, the migration
   that added the key repaired existing collisions first: per
   (event type, identity) it kept the row traffic most recently landed on
   (`last_seen_at` desc, then `created_at` asc — the winner rule
   `generate_events` already uses) and left every other row in place with its
   identity suffixed ` #duplicate-<event id>` and a `duplicate-identity` tag.
   It deleted and merged nothing.
7. The run retires the scan-created variables nothing refers to any more
   (`worker/variable_sweep`), after the commit and before the search reindex, so
   the reindex sees the retired set and a later failure cannot roll the
   deletions back. `run_scan` does this unconditionally. `collect_metrics`,
   whose Phase 1 mints variables through this same pipeline, does it on every
   run that is **not `is_replay`** — the same flag that already guards the sync
   and the reindex; a replay skips catalog sync entirely, so it holds no fresh
   evidence about which paths a row still carries and is in no position to call
   a variable unused — and lets **whether the catalog window was DECLARED**
   decide how much of the catalog that sweep may judge (tripl-bh1q, tripl-bwo8).
   The sweep is project-wide, so a scalar-derived variable is deferred whenever
   **any** scan config in that project has no declared `scan_lookback_hours`,
   even when the current scan uses a full-table query or declares its own
   lookback:

   - a **JSON-derived** variable — `source_name` a path whose dotted prefix
     names a `FieldDefinition` that is `json`-typed on every event type of the
     branch declaring it (`variable_retirement.is_json_derived` over
     `variable_sweep._json_column_names`) — is judged on every run;
   - a **scalar-derived** variable is judged only when the current collection
     has a declared lookback and every project config declares one; otherwise
     `retire_unused_variables` leaves it in place,
     unjudged, and counts it as `deferred` in the log. On this path the catalog
     view is *always* windowed (the task returns early without a `time_column`)
     and the fallback is `(time_from_dt, time_to_dt)`, one interval in steady
     state. `run_scan` has no such fallback: an unset lookback leaves its window
     `None` and it reads the whole table, but its project-wide sweep still
     defers scalar-derived variables when a sibling config has no lookback.

   The split exists because a too-narrow view does not merely *mis-report* — it
   rewrites the evidence the sweep reads, and how much it rewrites depends on
   what the variable was minted from. For a scalar column `plan_column_meta`
   sets `meta['is_low']` from the window's cardinality, `plan_events` emits a
   LITERAL instead of the `${token}` template on every event of the type at
   once, `_upsert_field_values` rewrites the stored values in place, and
   `delete_variable_contexts_for_event_type` drops that field's contexts because
   the run rewrote it — leaving a live variable with no token and no context,
   which is exactly `plan_retirement`'s definition of retirable. That rewrite
   predates the sweep and loses the observed-value history with or without one;
   what a sweep adds is recycling the row under a new id on the column's next
   busy hour, and a declared lookback is the operator saying the view is wide
   enough to own that. The `is_low` arm cannot reach a JSON column: a narrow
   window drops a key absent this interval from the value rebuilt for that one
   event, which is the "key that stopped arriving" the sweep exists for, and the
   key's return mints the variable again. The cost of the scalar gate is real
   and documented for users: `scan_lookback_hours` is nullable and defaults to
   `None` in every request schema — the create page pre-fills 24, a config saved
   without one shows the field blank — so a config that never had one typed into
   it prevents scalar-derived retirement across the project until every config
   has a declared lookback.

   `collect_metrics` also stamps the count onto `ScanJob.result_summary` the
   moment the delete commits, ~400 lines before the full summary is assembled.
   Everything in between — the reindex, per-chunk warehouse queries under a 24h
   soft limit, anomaly recalculation, alert preparation — can raise, and the
   deletions survive that; without the stub such a run reported failure and said
   nothing about what it had destroyed. A successful run overwrites the stub.
   `variables_retired` is therefore **absent** only on a replay, the one run
   that did not sweep, so a reader cannot mistake "did not look" for "found
   nothing"; every other run emits it, `run_scan` unconditionally, where `0`
   honestly means "swept, found nothing" — and whenever the project-wide gate
   defers scalar-derived variables, "swept" covers the JSON-derived rows alone.
7. `ScanJob.result_summary` is filled in for the UI.

Steps 4 and 5 are two modules, not one. `core/analyzers/event_plan.plan_events`
is the **pure** half: it turns breakdown rows into event identities by applying
the name format, the group rules and the cardinality collapse, and it touches no
`Session`. `core/analyzers/event_generator.generate_events` is the persistence
tail: it calls `plan_events` and then materialises the plan — variables, field
values, variable contexts, merges. The split exists so a *dry run* can ask the
question without answering it in a second implementation: `generate_events`
persists at eleven sites and cannot be made not to with a flag, and a
savepoint-and-rollback was rejected because it really executes `session.delete()`
on events and metrics.

Two invariants the split must preserve:

- The **reserved column set** is computed by the caller
  (`worker/utils/reserved_columns.reserved_catalog_columns`) and passed down.
  `core` must never import `worker`, and re-deriving the set inside the planner
  is what took a production scan down for 200 consecutive runs.
- Variable creation is hoisted out of the column loop into an **ordered**
  `variables_needed` list. `ensure_variable` creates a variable with the first
  type it is asked for, so a `set` would make the stored type depend on hash
  order.

Step 7's predicate lives in `core/variable_retirement` and is shared verbatim
with the owner-only `POST /projects/{slug}/danger/retire-unused-variables`
service; the worker runs it on the sync `Session`, the endpoint on the
`AsyncSession`, and only the queries differ. A variable is retirable only when a
scan created it (`description` still the scan's provenance string, `bindings`
still `[source_name]`, `name` still one the scan could have derived from
`source_name` — a typed display name is a person's, kept like an edited
description), no human evidence sits on it (documented values, an
exclusion tombstone, a per-event override, value-drift triage), it has no
observed `VariableValue` context, **and** none of its tokens — `name`,
`source_name`, `bindings` — appears as `${token}` in any stored `EventFieldValue`
**or** `EventMetaValue`.

Two things about that last pair are load-bearing. Both value tables are read
because both accept a token but only the first produces a `VariableValue`
context (that model is keyed by `field_definition_id`), so reading field values
alone retires a variable referenced solely from a meta value —
`event_service._attach_template_warnings` reads both, and so must this. And the
context check and the token check are independent on purpose: a group-rule merge
can leave a variable that a live event value still names but that carries no
contexts at all, and a predicate resting on contexts alone would delete exactly
those.

`variable_service.list_variables` reuses the same predicate to answer
`usage=used|unused` on the list endpoint, rather than approximating it with a
zero-usage-count filter — the count under the Variables page's select-all
checkbox has to be the set a run would take, not a superset. It runs the pass
only when the filter is asked for.

### Scan dry-run flow

1. The api creates a `ScanDryRunJob` (table `scan_dry_run_jobs`) from either a
   saved `scan_config_id` or a draft, and answers `202`.
2. The `dry_run_scan_config_async` Celery task runs the same `GROUP BY ALL` a
   real scan runs, bounded by `sample_row_limit`.
3. It resolves the target event type(s) exactly as `run_scan` does, then calls
   `plan_events` — never `generate_events`.
4. `ScanDryRunJob.result_summary` is filled with the event names, the fields that
   would be added, the templated columns, and the three independent bounds
   (window, sample, event cap) the answer is subject to. Nothing is written to
   the plan.

Draft inputs live on the row rather than in a request payload for the same reason
`ScanPreviewJob` does it: the work is dispatched, and the worker must be able to
reconstruct the request without the caller still being there. A draft is
reconstituted as a **transient** `ScanConfig` — constructed, never added to the
session — so `reserved_catalog_columns` can be reused verbatim on it.

### Metrics flow

1. Beat schedules due-checks.
2. Due scans dispatch metrics collection. A scan is due when the later of its
   newest stored bucket and the window its last **completed** collection
   recorded falls behind the current interval boundary — so a run that found an
   empty window still counts as progress and waits for the next boundary instead
   of being re-dispatched on every 300 s tick.
3. Phase 1 syncs the event catalog through the scan pipeline, so a scheduled
   collection creates events and variables exactly as a manual scan does — and
   for that reason closes the phase with the variable sweep of the scan flow's
   step 7 (on every run that is not a replay; a declared catalog window widens
   it from the JSON-derived variables to the scalar-derived ones too), then the
   reindex. A replay skips this whole phase and both of its tails; an undeclared
   window narrows only the sweep, never the reindex.
4. Counts are aggregated into `event_metrics`.
 5. Anomalies are recalculated into `metric_anomalies`.
 6. Matching alert rules enqueue deliveries.

Metric and anomaly bucket timestamps are UTC-aware in application code,
including SQLite-backed tests. The six event/catalog metric, breakdown, and
anomaly bucket model columns use the same UTC conversion contract as production
PostgreSQL; this model change does not require a database migration. API series
and anomaly responses serialize these bucket instants as RFC 3339 timestamps
with an explicit UTC `Z` suffix, for example `2026-09-24T08:00:00Z`.

### Catalog metric flow

1. Beat (`check_metric_definitions_due`, ~300 s) finds active, due metrics.
2. `collect_metric_definitions` evaluates SQL metrics and composes event series;
   `collect_fact_metrics_batch` groups fact metrics by interval, then runs one
   multi-aggregate query per fact table for every compatible group. Manual fact
   collection expands from the selected metric to all active dependents before
   entering the same batch path.
3. Values upsert into `metric_values` / `metric_value_breakdowns`.
4. Metric-scope anomalies are recalculated into `metric_anomalies`.
5. Alert rules with `include_metrics` enqueue deliveries.

### Alert flow

1. Anomaly items are matched against rule configuration.
2. `AlertDelivery` and `AlertDeliveryItem` rows are written.
3. A Celery task sends the formatted notification.
4. Delivery status becomes `pending`, `sent`, or `failed`.

Separately, the weekly plan-digest beat task sends directly to every enabled
Slack/email destination on a non-demo project; it does not evaluate routing rules
or create a normal anomaly delivery.

### Branch and implementation-ticket flow

1. A branch snapshots plan objects and records review approvals against a plan
   hash; edits make older approvals stale.
2. Merge policy and event-type owner gates are checked before the three-way
   merge applies changes to `main`.
3. Search is reindexed after merge. If a project tracker is enabled, creating a
   Jira or Linear implementation ticket is best-effort and cannot roll back the merge.
4. A periodic worker polls open tickets; a done issue (Jira's Done category, a
   Linear `completed` state) promotes its covered events to `implemented`
   without downgrading a later lifecycle state.
5. Metrics collection moves an event with volume from `ready_for_dev` or
   `implemented` to `live` when its required fields are filled, stamps
   `first_seen_at` once, records a history entry with `event_changes.source =
   'scan'` (shown as `tripl (scan)`; `NULL` means a person) and comments on
   the ticket. This is a data fact on `main` and bypasses branch rules.
6. A daily beat task computes lifecycle findings (deprecated events still
   firing past their sunset date, silent successors), each keyed on the
   deprecated event; `include_lifecycle` rules alert on the open ones. Every
   scan run offers them as project-global alert candidates (no scan config),
   deduplicated project-wide, one alert per finding episode.

### Search flow

Plan changes, scans, branch merges, and metric, fact-table, scan-config and
alert-rule CRUD refresh `search_documents`. Reindexing diffs content hashes so
unchanged embeddings are preserved. PostgreSQL full-text/trigram ranking is
always available; optional provider embeddings add semantic ranking, and a
periodic chaser requeues old pending documents.

The first search of a branch that has never been indexed does **not** build the
index inline on the request. It enqueues
`tripl.worker.tasks.search.reindex_search_branch` and answers with whatever is
already stored — for a never-indexed branch that is an empty result, and the
branch is searchable from the next request.

---

## Storage & integrations

- **PostgreSQL** stores the plan, metrics, audit log, and alert deliveries.
- **Photo / attachment storage** is pluggable: local filesystem or **GCS**. In
  `compose.yaml` the local backend's files live in the `photos` named volume on
  `app`, which needs backing up alongside PostgreSQL's `pgdata18`.
- **Alert destinations**: Slack, Telegram, generic webhook, email (SMTP),
  **Jira** (REST v3 with an ADF body), and **Linear** (GraphQL).

---

## Observability (both opt-in)

- **Prometheus** — a `/metrics` endpoint, enabled with
  `PROMETHEUS_METRICS_ENABLED`, exposing scan, anomaly, alert-delivery,
  schema-drift, and Celery task counters and histograms. Compose provides a
  shared multiprocess directory for API and worker metrics and clears stale
  files at deployment startup. A Celery failure is counted once.
- **OpenTelemetry** — tracing for FastAPI + SQLAlchemy + Celery, enabled with
  `OTEL_EXPORTER_OTLP_ENDPOINT`. The production image includes the tracing
  dependencies; a blank endpoint leaves tracing disabled.

---

## See also

- **[CONTRIBUTING.md](https://github.com/vladenisov/tripl/blob/main/CONTRIBUTING.md)** — setup, commands, source tree,
  API surface.
- **[agent-api-guide.md](../integrate/agent-api-guide.md)** — the API contract for agents and
  scripts.
- **[concepts.md](../use/concepts.md)** — the same system in plain language.
