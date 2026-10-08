---
title: Warehouse capability matrix
---

# Warehouse capability matrix

tripl talks to a warehouse through one interface — `BaseAdapter` — and offers
ClickHouse, BigQuery, Databricks and PostgreSQL as external sources. Offering them is not the
same as guaranteeing they behave identically.

This page is the honest version. It states, per capability and per warehouse,
whether a path is **supported**, **bounded** (it works, but it does not see all
your data or all your settings), or **not yet** implemented — and, separately and
just as importantly, **how each guarantee was verified**. Silent reductions are
not parity, and they are not documented as parity here.

The reference for everything below lives in code:

| Contract | Module |
| --- | --- |
| Interval codes | `backend/src/tripl/core/intervals.py` |
| Time windows and buckets | `backend/src/tripl/core/bucketing.py` |
| Column type classification | `backend/src/tripl/core/warehouse_types.py` |
| Dialect literals, quoting and the pre-flight lint | `backend/src/tripl/core/adapters/measure_validator.py` |
| The adapter surface itself | `backend/src/tripl/core/adapters/base.py` |
| The executable conformance gates | `backend/src/tripl/tests/conformance/` |

---

## Read this first: proven versus believed

PostgreSQL and ClickHouse execute every conformance layer. BigQuery is analyzed on
every PR and has additionally passed a credentialed adapter-level value suite on
real BigQuery. A trusted-release workflow reruns that suite and the full worker
pipeline for each `vX.Y.Z` release tag once credentials are configured. Pull
requests remain credential-free and stop at ZetaSQL analysis.

| Warehouse | How CI verifies it | What that authorizes | What it does **not** authorize |
| --- | --- | --- | --- |
| **ClickHouse** | **EXECUTED.** A real `clickhouse-server:26.8` container runs the SQL the adapter generates and the results are compared against the reference implementation. | SQL validity **and** computed values: bucket timestamps, counts, aggregates, nested paths, contract counts. | — |
| **PostgreSQL** | **EXECUTED.** A real `postgres:18` container runs the SQL the adapter generates and the results are compared against the reference implementation. | SQL validity **and** computed values, exactly as ClickHouse. | — |
| **BigQuery** | **ANALYZED on every PR; values executed on trusted releases.** The emulator's real ZetaSQL analyzer checks every generated statement. A credentialed job runs for `vX.Y.Z` tags when explicitly enabled. | SQL validity plus exact adapter values; the release gate also compares scan/replay event series, fact and composition metrics, batched collection, idempotency and anomalies against the shared reference while using real PostgreSQL for application state. | Credentialed checks run only on release tags to bound quota usage. |
| **Databricks** | **Mocked on every PR, executed on release tags.** `test_databricks_adapter.py` and the shared parity suites drive the real adapter against a fake DB-API driver and assert the SQL it sends and how it decodes what comes back. The credentialed value suite (`conformance/test_databricks_value_conformance.py`, marker `databricks_value`) runs against a real SQL warehouse in `databricks-value-conformance.yml` on stable release tags, and passed by hand against a Free Edition warehouse when the connector shipped. | Everything the value suite covers: bucket counts on every interval over TIMESTAMP, TIMESTAMP_NTZ and DATE, half-open windows, sums and breakdowns, multi-aggregates, top-N folding, VARIANT and STRUCT paths, JSON in STRING columns, field-contract counts. | That a pull request keeps Databricks working: between release tags the evidence is the mocked SQL. Cells the value suite does not reach stay believed. |
| **Snowflake** | **Mocked on every PR; values executed on release tags once an account is configured.** `test_snowflake_adapter.py` and the shared parity suites drive the real adapter against a fake DB-API driver and assert the SQL it sends and how it decodes what comes back. The credentialed value suite (`conformance/test_snowflake_value_conformance.py`, marker `snowflake_value`) runs against a real account in `snowflake-value-conformance.yml` on stable release tags when `SNOWFLAKE_VALUE_CONFORMANCE_ENABLED` is set. It has not yet been run against a live account. | Today: the SQL shape and the result decoding. Once the value suite has run: bucket counts on every interval over TIMESTAMP_TZ, TIMESTAMP_NTZ, TIMESTAMP_LTZ and DATE, half-open windows, sums and breakdowns, multi-aggregates, top-N folding, VARIANT paths, JSON in STRING columns, field-contract counts. | That Snowflake accepts the SQL or computes the reference values: until the value suite passes against a real account, every Snowflake cell is believed, not proven. |
| **Greenplum** | **EXECUTED.** The PostgreSQL gates (`test_postgres_conformance.py`, `test_postgres_field_contracts_conformance.py`) run again through `GreenplumAdapter` against real Greenplum **6.27** and **7.1** containers (`woblerr/greenplum`), in the `greenplum` CI job. | SQL validity **and** computed values, as PostgreSQL, on both major versions. | The pipeline gate (it keeps tripl's own schema in the same database) and the forks (Cloudberry, Greengage, WarehousePG), which share Greenplum's SQL but are not run. |
| **Amazon Redshift** | **Mocked on every PR; values executed on release tags once an endpoint is configured.** `test_greenplum_redshift_adapters.py` and the shared parity suites assert the SQL `RedshiftAdapter` sends. The credentialed value suite (`conformance/test_redshift_value_conformance.py`, marker `redshift_value`) runs against a real Serverless workgroup or cluster in `redshift-value-conformance.yml` on stable release tags when `REDSHIFT_VALUE_CONFORMANCE_ENABLED` is set. It has not yet been run against a live endpoint. | Today: the SQL shape. Once the value suite has run: bucket counts on every interval over `timestamptz` and `timestamp`, half-open windows, sums and breakdowns, multi-aggregates with CASE-folded filters, top-N folding, field-contract counts, literal quoting. | That Redshift accepts the SQL or computes the reference values: until the value suite passes against a real endpoint, every Redshift cell is believed, not proven. No JSON: Redshift sources do not support JSON columns. |
| **Trino** | **EXECUTED.** The value suite (`conformance/test_trino_value_conformance.py`, marker `trino_value`) runs against a real `trinodb/trino` **483** coordinator in the `trino` CI job, over a table-less fixture built from `VALUES`. `test_trino_adapter.py` and the shared parity suites pin the SQL on every PR as well. | SQL validity **and** computed values: bucket timestamps on every interval over `timestamp(6) with time zone`, `timestamp(3) with time zone`, `timestamp` and `date`, half-open windows, sums and breakdowns, multi-aggregates, top-N folding, `json` paths, shapes and properties, JSON in `varchar` columns, field-contract counts, regex probing, literal quoting, the coordinator-side timeout. | Starburst's own additions and the connectors behind a catalog (Hive, Iceberg, Delta): the fixture is table-less, so the SQL is proven, not every connector's type mapping. |
| **Amazon Athena** | **Mocked on every PR; values executed on release tags once an account is configured.** `test_athena_adapter.py` drives the real adapter against a fake `pyathena` connection; its SQL is the Trino adapter's, which the `trino` job executes. The credentialed value suite (`conformance/test_athena_value_conformance.py`, marker `athena_value`) runs the same tests as Trino's in `athena-value-conformance.yml` on stable release tags when `ATHENA_VALUE_CONFORMANCE_ENABLED` is set. It has not yet been run against a live account. | Today: the SQL (through Trino) and the driver wiring. Once the value suite has run: the same cells as Trino, on Athena engine version 3. | That Athena's engine (version 3, a Trino fork) agrees with upstream Trino on every function tripl uses: until the value suite passes against a real account, Athena-specific behaviour is believed, not proven. |
| synthetic | In-memory fixture, not a warehouse. | Nothing about a real warehouse. | — |

**Why emulator values are never used.** The emulator's *analyzer* is Google's;
its *evaluator* is not. It computes some expressions wrongly —
demonstrated: `DATETIME_TRUNC(DATETIME '2026-04-08 13:00:00', WEEK(MONDAY))`
returns `2026-04-06T13:00:00` on the emulator, wrongly keeping the time
component, where real BigQuery returns `2026-04-06T00:00:00`. Asserting values
against it would produce either a false failure or — far worse — a false PASS
certifying a bucket contract the emulator itself got wrong. So the gate asserts
exactly one thing: every generated statement analyzes.

**Therefore, on BigQuery:**

- **Proven:** every generated statement is valid GoogleSQL. `TIMESTAMP_BUCKET` /
  `DATETIME_BUCKET` / `DATE_BUCKET` resolve, `*_TRUNC(…, WEEK(MONDAY))` resolves,
  `JSON_KEYS(doc, 20)` resolves, the `GROUPING SETS` shape resolves, and no query
  groups by an ARRAY. (Each of these was a real defect; see
  [What was broken and is now fixed](#what-was-broken-and-is-now-fixed).)
- **Proven on real BigQuery:** `TIMESTAMP`, `DATETIME` and `DATE` bucket values,
  Monday 00:00 weeks, half-open membership, counts, sums, breakdowns,
  multi-aggregates, nested JSON/STRUCT values and field-contract counts all match
  the same pure-Python reference used by PostgreSQL and ClickHouse.
- **Release-gated on real BigQuery:** the end-to-end worker pipeline compares stored
  event series, fact and composition metrics, batched collection, replay idempotency
  and expected anomalies with the same reference used by PostgreSQL and ClickHouse.

The credentialed job is `bigquery-value-conformance.yml`. It runs only on trusted
`vX.Y.Z` release tags, uses table-less fixtures requiring only `bigquery.jobUser`,
keeps worker state in an ephemeral PostgreSQL service, caps each query, and fails if
any selected test skips. `BQ_VALUE_CONFORMANCE_ENABLED=true`
also makes missing project/credentials a hard configuration error instead of a
green no-op.

Databricks has the same shape in `databricks-value-conformance.yml`: stable
`vX.Y.Z` tags (or a manual run), the `DBX_TOKEN` secret with the `DBX_HOST` and
`DBX_HTTP_PATH` variables (`DBX_CATALOG` defaults to `workspace`), switched on by
`DBX_VALUE_CONFORMANCE_ENABLED=true`, and a failure on any skip. Its fixtures are
table-less too, so the token needs only to run statements on the warehouse.

The CI job is `conformance` in `.github/workflows/ci.yml`. It fails if a
conformance test **skips** — a gate that quietly skips because a warehouse was
unreachable is a gate that tested nothing while reporting green.

---

## The semantic contract

Every adapter translates the same dialect-neutral request into its own SQL. The
translation is only correct if it agrees with the canonical definitions below —
not "looks similar to", but produces the same bucket for the same UTC input.

### Everything is UTC

A naive `datetime` is *assumed* to already be UTC and is stamped as such; an
aware one is converted. The worker's `TZ`, the warehouse server's timezone and
the database role's `timezone` setting must never decide which bucket a row lands
in. Adapters pin the session or column timezone to UTC rather than inheriting the
server's, and window bounds are rendered with an explicit `+00:00` offset — an
offset-less literal is read in the *session* timezone by some dialects, which is
exactly the silent window shift the contract exists to prevent.

The conformance gates cover this directly: PostgreSQL and ClickHouse are both
driven with a **non-UTC server** and a **non-UTC column** and must still produce
the same buckets.

Because the contract is UTC-only there is no DST hazard anywhere in it: UTC has
no DST transitions, so a fixed-width bin can never straddle a clock change.

### Windows are half-open

`time_from <= t < time_to`. A row landing exactly on `time_to` belongs to the
*next* window. Adjacent windows therefore tile without double-counting the
boundary row — which matters for replay, where consecutive chunks share an edge.

### Interval codes, not dialect syntax

Callers pass a code, never dialect syntax. The supported codes are the product's
whole interval vocabulary:

| Code | Meaning | Bucket origin |
| --- | --- | --- |
| `15m` | Every 15 minutes | Unix epoch |
| `1h` | Every hour | Unix epoch |
| `6h` | Every 6 hours | Unix epoch |
| `1d` | Every day | Unix epoch |
| `1w` | Every week | **Monday** (`1970-01-05T00:00:00Z`) |

### Sub-week buckets are epoch-anchored; week buckets start on Monday

`15m`, `1h`, `6h` and `1d` all divide a UTC day evenly, so anchoring them at the
Unix epoch also puts every boundary on a natural clock boundary.

Weeks are the one place the warehouses disagree by default, and it is a trap:
**1970-01-01 was a Thursday**, so a naive seven-day bin off the epoch starts
weeks on a Thursday — which is exactly what PostgreSQL, BigQuery and the frontend
all used to do. Each adapter now says "Monday" *explicitly*, whether or not its
dialect default already agrees:

| Warehouse | Week expression | Verified |
| --- | --- | --- |
| ClickHouse | `toDateTime(toMonday(col, 'UTC'), 'UTC')` — ClickHouse is the one whose default already agrees: `toStartOfInterval(col, INTERVAL 1 WEEK)` is Monday-anchored at `1970-01-05`, *not* off the epoch Thursday. `toMonday` is used for a different reason — the week form of `toStartOfInterval` returns a **Date**, so a `1w` bucket would come back as `datetime.date` while every other interval yields `datetime.datetime` | **executed** |
| PostgreSQL | `date_bin('7 days', col, TIMESTAMPTZ '1970-01-05 00:00:00+00:00')` — anchored at the first Monday, not the epoch | **executed** |
| Greenplum, Redshift | No `date_bin` (Greenplum is PostgreSQL 9.4 / 12, Redshift 8.0), so every interval is epoch arithmetic: `origin + floor((date_part('epoch', t) - origin_seconds) / w) * w * INTERVAL '1 second'`, with the origin at the epoch, or at `WEEK_ORIGIN` (a Monday) for weeks — the grid `date_bin` draws. The session time zone is pinned to UTC, so a `timestamp` column's wall clock reads as UTC | Greenplum **executed** (6 and 7); Redshift **not executed** (mocked) |
| Trino, Athena | `1h`/`1d` use `date_trunc` on the UTC wall clock (a zoned column is moved `AT TIME ZONE 'UTC'` first, because `date_trunc` truncates in the value's own zone). `15m`, `6h` and weeks floor the whole seconds since the epoch — `date_diff('second', TIMESTAMP '1970-01-01 00:00:00.000', date_trunc('minute', t))` — onto a grid anchored at the epoch, or at `WEEK_ORIGIN` (a Monday) for weeks; no `day_of_week` setting is read. Every bucket is a zone-less `timestamp(3)` holding the UTC wall clock | Trino **executed**; Athena **not executed** (mocked) |
| BigQuery | `TIMESTAMP_TRUNC(col, WEEK(MONDAY), 'UTC')` / `DATETIME_TRUNC(col, WEEK(MONDAY))` / `DATE_TRUNC(col, WEEK(MONDAY))` by declared time type | **executed on real BigQuery** for all three time families |
| Databricks | `date_trunc('WEEK', CAST(col AS TIMESTAMP))` — Databricks documents `WEEK` truncation as the Monday of the week. `15m`/`6h` use `timestamp_seconds(floor(unix_seconds(t) / w) * w)` off the epoch; `1h`/`1d` use `date_trunc`, the same grid because they divide the UTC day. The session time zone is pinned to UTC | **not executed** (mocked) |
| Snowflake | Weeks, `15m` and `6h` floor `DATE_PART(EPOCH_SECOND, t)` onto a grid anchored at the epoch (at `WEEK_ORIGIN`, a Monday, for weeks), so the session's `WEEK_START` cannot move a bucket; `1h`/`1d` use `DATE_TRUNC`. `TIMESTAMP_TZ`/`TIMESTAMP_LTZ` are converted to the UTC wall clock first, because `DATE_TRUNC` on a `TIMESTAMP_TZ` truncates in the value's own offset. The session time zone is pinned to UTC | **not executed** (mocked) |

`floor_to_bucket(value, code)` in `core/bucketing.py` is the definition all of them
are measured against.

The same origins govern the **window**, not only the bucket. `_floor_to_interval`
/ `_ceil_to_interval` in `worker/tasks/metrics/_helpers.py` — which produce the
`[time_from, time_to)` bounds a collection or a replay hands the adapter, and the
"latest complete interval" boundary `check_metrics_due` compares a scan against —
bin on `floor_to_bucket`'s grid, weeks from `WEEK_ORIGIN` included. They used to
anchor *every* interval at 2000-01-01, which is a **Saturday**, so every weekly
bound they produced fell five days into a bucket instead of on its edge. Two
consequences followed, both of them silent. A scheduled run ended on a Saturday,
so the newest week it wrote held Monday through Friday and nothing more until the
next run widened it. And because a chunk replaces the buckets inside its own
window, a run chunked by `replay_chunk_interval` split every Monday bucket across
two chunks — the second chunk's two-day tail overwrote the five days the first
had written. A weekly window now opens and closes on a Monday, so the bounds, the
chunk edges and the buckets inside them all describe the same weeks.

### Supported time types

A time column must carry a date. Anything that does not — a time-of-day type —
cannot be placed in a window at all.

| Warehouse | Supported | Rejected | Rejected at configuration time? |
| --- | --- | --- | --- |
| ClickHouse | `DateTime`, `DateTime64`, `Date`, `Date32` | — | n/a |
| BigQuery | `TIMESTAMP`, `DATETIME`, `DATE` | `TIME` | **Not guaranteed** — the adapter raises an actionable error naming the column and its type, but only where the column's time kind is first needed: a bucket expression or a window predicate. A scan preview builds a window predicate only when the config carries a lookback window, so a scan saved without one first fails on a run. See caveat [7] |
| Databricks | `TIMESTAMP`, `TIMESTAMP_NTZ`, `DATE` | anything else (`INTERVAL`, arrays) | **Not guaranteed** — like BigQuery, refused with an actionable error where the time kind is first needed. A `DATE` column refuses `15m`/`1h`/`6h` |
| Snowflake | `TIMESTAMP_TZ`, `TIMESTAMP_LTZ`, `TIMESTAMP_NTZ`, `DATE` | `TIME`, anything else | **Not guaranteed** — refused with an actionable error where the time kind is first needed. A `DATE` column refuses `15m`/`1h`/`6h` |
| PostgreSQL | `timestamp`, `timestamptz`, `date` | `time`, `timetz`, and any array (`timestamptz[]`) | **No** — classified as unsupported, but not acted on. See caveat [7] |
| Greenplum, Redshift | As PostgreSQL | As PostgreSQL | As PostgreSQL |
| Trino, Athena | `timestamp(p)`, `timestamp(p) with time zone`, `date` | `time`, `time with time zone`, anything else | **Not guaranteed** — refused with an actionable error where the time kind is first needed. A `date` column refuses `15m`/`1h`/`6h` |

Notes that bite in practice:

- BigQuery `DATETIME` is a zone-*less* wall clock; a `TIMESTAMP`-typed literal
  compared against it is rejected by GoogleSQL. The adapter picks the bucket
  function *and* the literal type from the column's declared kind
  (`TIMESTAMP_BUCKET` / `DATETIME_BUCKET` / `DATE_BUCKET`, and `TIMESTAMP '…'` /
  `DATETIME '…'` / `DATE '…'`).
- **A BigQuery `DATE` column cannot use a sub-day interval.** `15m`, `1h` and `6h`
  are meaningless on a column with no time-of-day, and are refused with an
  actionable error rather than silently collapsed to a day. Use `1d` or `1w`, or
  a `TIMESTAMP`/`DATETIME` column.
- ClickHouse `DateTime`/`DateTime64` carry a timezone, so bucketing must pass
  `'UTC'` explicitly — `toStartOfInterval` otherwise buckets in the *column's*
  timezone.

### Nested paths

`classify_complex` decides how a column's nested values are addressed. It is
**case-insensitive**, because psycopg reports PostgreSQL's types in lowercase and
a case-sensitive `"JSON"` substring match classified every PostgreSQL JSON column
as a plain scalar — which is why JSON preview, discovery and path extraction never
activated on PostgreSQL at all.

| Kind | Meaning | Dialect spellings |
| --- | --- | --- |
| `json` | Schemaless document; paths are discovered *from the data* | CH `JSON` / `Object('json')`, BQ `JSON`, PG `json` / `jsonb`, Databricks `VARIANT`, Trino/Athena `json` |
| `struct` | Fixed nested schema; paths come from the *declared schema* | BQ `RECORD` / `STRUCT`, CH `Tuple(…)`, Databricks `STRUCT<…>` |
| `map` | Key/value container | CH `Map(…)`, Databricks `MAP<…>` |

Path rules:

- A path is a dot-separated chain of identifier-safe parts (`a.b.c`). Parts that
  are not identifier-safe are rejected, not escaped — the path is interpolated
  into SQL, so the allowlist is also a security boundary.
- **Dotted nested leaf paths are served by *discovery* on all three warehouses** —
  ClickHouse via `arraySort(JSONAllPaths(col))`, PostgreSQL via a recursive
  `jsonb_each` walk, BigQuery via `JSON_KEYS(col, 20)` reduced to its leaf set. All
  three surface `user.address.city`, not just `user`. This is the enumeration the UI
  shows you and the one you pick paths from. BigQuery stops at **depth 20** (caveat
  [5]), and on ClickHouse this covers `JSON` columns only — a `Map` or `Tuple`
  column is not enumerated and offers no selectable path (caveat [8]).
- **Scan-time *shape grouping* differs, and PostgreSQL is deliberately coarser.**
  The scan groups each row by its path set to count distinct document shapes, so that
  expression runs once per row across the whole window. ClickHouse gets nested paths
  there for free on a `JSON` column — `JSONAllPaths` is a columnar metadata read;
  a `Map` groups on the row's key set and a `Tuple` on its declared field names,
  because `JSONAllPaths` rejects both (caveat [8]). PostgreSQL has no such
  primitive: the equivalent recursive walk costs **~44 µs/row**, measured on
  PostgreSQL 18 over a 30k-row, 4-level fixture:

  | scan | time (30k rows) |
  |---|---|
  | scalar columns only | 16 ms |
  | top-level keys (what PostgreSQL now does) | 245 ms |
  | nested leaf paths | 1744 ms — **7×** the above, **108×** the floor |

  It is irreducible rather than a coding mistake: `EXPLAIN` shows the recursive CTE
  running with `loops=30000` (once per row), a LATERAL rewrite is slower (1847 ms),
  an unrolled depth-limited expansion is slower still (2976 ms), and the walk *alone*
  with no grouping already costs 1320 ms. On a 10M-row window that is minutes of CPU
  spent purely on parsing paths.

  So PostgreSQL groups scans on **top-level keys**, and its "distinct shapes" count is
  coarser than ClickHouse's. Nested paths are unaffected everywhere they are actually
  used: discovery, extraction, and metrics over a chosen path. This is a real
  divergence, stated here rather than passed off as parity.
- Path *discovery* (the preview-time "what keys does this column have" probe) is
  bounded on **all three** by a source-row sample — see caveat [4]. It is a
  different operation from scan-time enumeration, with a different bound.
- **An array of a nested type is not nested.** psycopg reports a PostgreSQL array
  column as `jsonb[]` / `int4[]` / `timestamptz[]` — it used to report the
  *element's* name, so a `jsonb[]` column was read as `jsonb`, routed into the
  `jsonb` path walk, and the `::jsonb` cast that walk emits failed the whole scan.
  An array now classifies as an opaque scalar, which is what ClickHouse
  `Array(JSON)` and BigQuery `REPEATED` already did; it still groups and still
  renders as text. An array of a *time* type (`timestamptz[]`) is classified
  unsupported for the same reason — but on PostgreSQL that classification is not
  acted on at configuration time (caveat [7]).
- BigQuery `STRUCT`/`RECORD` columns are now extractable via dotted field access,
  with one exclusion: a leaf underneath a **REPEATED** field needs `UNNEST`, which
  the adapter does not generate, and is rejected loudly. ClickHouse `Tuple`/`Map`
  columns are shape-enumerated by the scan but have **no value extractor** — no
  dotted path under them is selectable (caveat [8]).

### Top-N breakdowns rank over the whole collection window

A breakdown with a values limit keeps its top values and folds the rest into
`Other`. Every engine ranks those values by row count over the **whole window
of one collection**, not over each chunk of it, so a chunked replay keeps one
set of explicit values from its first chunk to its last. "The window" is always
the window of the metric (or scan config) being written. The batched fact path
shares one breakdown scan between metrics, but a limited breakdown shares it
only with metrics that have the same window, and ranks over that window. The set
a metric keeps therefore does not depend on which other metrics in its group
happen to be behind, and it matches the per-metric collectors.

Chunking (`replay_chunk_interval`) bounds the bucketed scans, **not** this
ranking. The ranking pre-query is the one statement of a chunked collection that
reads the whole window. It is a single `GROUP BY` over the breakdown column,
with no time buckets and no aggregates, and it runs once per collection rather
than once per chunk. It is still a scan of the whole range, so on a very long
replay over a large table it can be the statement that reaches the data
source's timeout (see [The query timed out](#the-query-timed-out)).

### Exact versus bounded

The distinction this page turns on:

- **Exact** — the warehouse aggregates the *entire* configured window. The answer
  does not depend on how much data there is.
- **Bounded** — the path applies a row limit, a sample, a depth cap, or a
  single-scope filter. It is still useful, but a value past the bound is
  invisible, and the result is silently *plausible* rather than correct.

Bounded paths are marked in the matrix and footnoted. They are not parity.

---

## The matrix

Legend: **full** = exact, warehouse-side, no hidden reduction · **bounded** = works
but reduced, see footnote · **none** = not implemented.

Read every BigQuery cell through the [proven-versus-believed](#read-this-first-proven-versus-believed)
table above: adapter capabilities covered by the credentialed suite have exact
value proof; pipeline-derived capabilities execute in the credentialed release
gate while pull requests retain analysis-only coverage.

The `synthetic` column is the in-memory demo warehouse (`DBType.synthetic`). It
opens no socket, serves a bounded deterministic fixture (a 30-day history, hard
capped at 65,000 rows per table), and raises `SyntheticCapabilityError` rather
than fabricating an answer it cannot honestly compute. It is included because it must satisfy the same contract, not because it
is a shipping warehouse.

| Capability | Adapter surface | ClickHouse | BigQuery | Databricks | PostgreSQL | synthetic |
| --- | --- | --- | --- | --- | --- | --- |
| Connection test | `test_connection` | full | full | full | full [7] | full [10] |
| Schema browse (autocomplete) | `get_schema_tables` | full | **bounded [1]** | **bounded [14]** | full | full |
| Preview rows (time-windowed) | `get_preview_rows` | full | full | full | full | full |
| JSON path discovery (preview probe) | `get_json_path_samples` | **bounded [4]** | **bounded [4]** | **bounded [4]** | **bounded [4]** | bounded [4] |
| Nested path enumeration (scan) | `get_full_breakdown` | **full (JSON), shape-only for `Map`/`Tuple` [8]** | **bounded [5]** | **top-level only [14]** | **top-level only [6]** | full |
| Nested value extraction (selected paths) | all bucketed methods | full (JSON), none for `Tuple`/`Map` [8] | full (JSON + STRUCT [5]) | full (`VARIANT` + STRUCT + one `MAP` level [14]) | full (JSON) | full |
| Scan run / full breakdown | `get_full_breakdown` | full | full | full | full | full |
| Scan replay (chunked) | bucketed methods | full | full | full | full | full |
| Event generation | bucketed methods | full | full | full | full | full |
| Variables and bindings | derived from scan output | full | full | full | full | full |
| Event metrics (bucketed counts) | `get_time_bucketed_counts` | full | full | full | full | full |
| Event metric breakdowns (single) | `get_time_bucketed_breakdown_counts` | full | full | full | full | full |
| Event metric breakdowns (multi) | `…_breakdown_counts_multi` | full | full | full | full | full |
| Top-N + `Other` folding (ranked once over the caller's whole window, not per chunk) | `values_limit` on breakdown methods, `top_n_ranking_window` | full | full | full | full | full |
| SQL metrics (free-text) | `get_preview_rows` | full [9] | full [9] | full [9] | full [9] | bounded [10] |
| SQL metric starter templates | frontend `metricTemplates.ts` | full | full | full | full | n/a |
| Dialect pre-flight lint (metric preview and metric save [9]) | `lint_dialect_sql` | full | full | full | full | full |
| Fact metrics (aggregate) | `get_time_bucketed_aggregate` | full | full | full | full | full |
| Fact metric breakdowns | `get_time_bucketed_aggregate_breakdown` | full | full | full | full | full |
| Fact ratio metrics (one scan) | `get_time_bucketed_multi_aggregate` | full | full | full | full | full |
| Fact ratio breakdowns | `…_multi_aggregate_breakdown` | full | full | full | full | full |
| Structured fact filters | `AggregateSpec.filter_sql` | full | full | full | full | bounded [10] |
| Schema drift | derived from scan output | full | full | full | full | full |
| Value / distribution drift | derived from scan output | full | full | full | full | full |
| Properties as breakdowns, drift fields and contracts (`<json_column>.<path>`, F23) | `_field_value_expression` / `_field_operand` | full: JSON subcolumn; one `Map` key and named `Tuple` element by hand only, not execution-verified [8] | full: `JSON_VALUE` (JSON), declared STRUCT field | full: `` `c`:['a']['b'] `` (`VARIANT`), declared STRUCT field, one `MAP` key | full: `#>>` (`json`/`jsonb`) | **none** — no JSON columns; a property breakdown raises `SyntheticCapabilityError` [10] |
| Text columns parsed as JSON (`json_string_columns`, F23.9) | `json_string_source`, resolved once per adapter by `core.json_string_columns` | full: `SELECT * REPLACE (CAST(if(isValidJSON(s) AND JSONType(s) = 'Object', s, '{}'), 'JSON') AS c)` — needs the `JSON` type (25.x) | full: `SELECT * REPLACE (IF(JSON_TYPE(SAFE.PARSE_JSON(c, wide_number_mode => 'round')) = 'object', …, NULL) AS c)` | full: `CASE WHEN startswith(schema_of_variant(try_parse_json(c)), 'OBJECT') THEN try_parse_json(c) END` — needs `VARIANT` (Databricks SQL, DBR 15.3+) | **none** — no non-failing text→`jsonb` cast before PG 16; the API refuses the setting (`422`) and the adapter raises `WarehouseCapabilityError` | **none** — refused like PostgreSQL |
| **Field contracts** (required/enum/regex/range) | `validate_field_contracts` | **full** | **full** (warehouse-side, full window) | **full** (warehouse-side, full window, one scan; Java regex, see "Regex contracts") | **full** (warehouse-side, full window; range compares in exact decimal, see "PostgreSQL range contracts compare exactly") | bounded [10] |
| Anomaly detection | none (post-hoc) | full [11] | full [11] | full [11] | full [11] | full [11] |
| Alerts | none (post-hoc) | full [11] | full [11] | full [11] | full [11] | full [11] |
| Query timeout | data source `timeout_seconds` | full | full [2] | full [14] | full | **n/a — accepted and ignored [10]** |
| In-flight query cancellation | adapter | **bounded [12]** | **bounded [12]** | **bounded [12]** | **bounded [12]** | bounded [12] |
| Cost / billed-bytes guard | `maximum_bytes_billed` | n/a | full [3] | n/a | n/a | n/a |
| TLS enforcement | connection settings | full (HTTPS port) | full (Google TLS) | full (HTTPS 443, certificate verified) | full [13] | n/a |
| Executable SQL conformance | `tests/conformance/` | **executed** | **release-gated execution; analyzed on PRs** | **release-gated execution; mocked on PRs [14]** | **executed** | n/a |

---

## Caveats

**[1] BigQuery schema browse spans the default dataset plus an explicit
allowlist, and nothing else.** ClickHouse introspects every non-system *database*
and PostgreSQL every non-system *schema* in a single catalog query. BigQuery
cannot: `INFORMATION_SCHEMA.COLUMNS` is dataset-qualified, so each dataset costs
its own job. The browse therefore covers the connection's default dataset plus
any datasets in the source's **dataset allowlist**, with three hard bounds: at
most **20 datasets**, at most **50,000 catalog rows across all of them combined**
(a shared budget, not a per-dataset allowance), and a **30-second cap** per
introspection job. That 20 is why the allowlist field itself accepts at most
**19**: the connection's default dataset always takes the first slot, so a
longer list would be one the browse could never honour.
Names inside the default dataset come back bare (`events`);
names outside it come back qualified (`analytics.orders`), matching the
ClickHouse/PostgreSQL convention the frontend depends on. A dataset the
credentials cannot read is logged and skipped — the rest still return their
tables — but a browse in which *every* dataset failed re-raises rather than
returning an empty catalog that looks like "this project has no tables".
Tables in a dataset that is neither the default nor allowlisted are invisible to
autocomplete; they still work if you type them.

**[2] BigQuery honors the query timeout, in two places.** It previously had
**none at all** — a pathological `base_query` pinned a Celery worker until the
55-minute hard limit killed it. It is now bounded on both sides of the wire:
client-side by a `job.result(timeout=…)` deadline, and server-side by
`job_timeout_ms` on the client's default job config, so BigQuery abandons the job
even if the worker is SIGKILLed before it can react. On timeout the job is
**cancelled best-effort** (`job.cancel()`), because a BigQuery job outlives the
client that started it and would otherwise keep scanning — and billing — after
tripl has given up on it. A cancel that itself fails is logged, never allowed to
mask the timeout the caller needs to see. The deadline is the data source's
`timeout_seconds` (default **300s**).

**[3] BigQuery has a cost guard, on by default.** Every job carries
`maximum_bytes_billed`, defaulting to **100 GiB** per query and configurable per
data source. BigQuery **refuses** a query whose estimate exceeds it, before a byte
is billed — so a stray cross join in a `base_query` is bounded by tripl rather
than by your GCP invoice. Raise it deliberately if a legitimate scan needs more.

**[4] JSON path *discovery* is sampled on every warehouse.** The preview-time
probe that populates the "which JSON paths does this column have" picker is
bounded by **1,000 source rows** (`sample_row_limit`), **1,000 distinct paths**
(`path_limit`) and **3 sample values per path** (`sample_limit`), on ClickHouse,
BigQuery and PostgreSQL alike. ClickHouse and PostgreSQL enumerate the paths
**warehouse-side** within that sample (`JSONAllPaths`/`JSONDynamicPaths` — for
ClickHouse `JSON` columns only, caveat [8] — and a recursive `jsonb_each` walk
respectively), so they see every nested leaf in the sampled rows at a fraction of
the transfer; BigQuery inherits the `BaseAdapter`
fallback, which pulls the sampled rows back and flattens them in Python. Either
way: **a key present in 0.01% of your events will usually not be discovered.**
The bound is on discovery only — scan-time enumeration and extraction are not
sampled.

**[5] BigQuery nested enumeration stops at depth 20, and STRUCT leaves under a
REPEATED field are not addressable.** `JSON_KEYS(col, 20)` has no "unlimited"
argument, so a leaf below 20 levels is not enumerated. Separately, a STRUCT leaf
nested inside an `ARRAY<STRUCT<…>>` cannot be reached by dotted field access in
GoogleSQL — it needs `UNNEST`, which this adapter does not generate. Such leaves
are still *enumerated* (so they stay visible in discovery) but are rejected with
an actionable error if selected, rather than compiled into SQL that fails opaquely
inside a worker.

**[6] PostgreSQL groups scans on top-level keys, not nested leaves — measured, and
deliberate.** The scan's path expression runs once per row over the whole window.
ClickHouse gets nested paths there for free (`JSONAllPaths` is a columnar metadata
read); PostgreSQL has no equivalent, and the recursive `jsonb_each` walk that
produces the same answer costs **~44 µs/row** — on a 30k-row, 4-level fixture on
PostgreSQL 18: 16 ms for a scan with no JSON, 245 ms for top-level keys, **1744 ms**
for nested leaves. That is 7× the top-level form and 108× the floor; on a 10M-row
window it is minutes of CPU spent only on parsing paths.

The cost is irreducible, not a coding mistake: `EXPLAIN` shows the recursive CTE at
`loops=30000` (once per row), a LATERAL rewrite measures *slower* (1847 ms), an
unrolled depth-limited expansion slower still (2976 ms), and the walk alone with no
grouping is already 1320 ms.

So the scan groups on top-level keys and PostgreSQL's **distinct-shapes count is
coarser than ClickHouse's**. Everything users actually reach for is unaffected:
nested-path *discovery*, *extraction*, and metrics over a chosen path all still see
`user.address.city`. A conformance test pins both depths, so restoring the nested
walk to the scan cannot silently reintroduce the regression.

**[7] PostgreSQL requires version 14 or newer, and `time` columns are still not
rejected at configuration time.** Every bucket query goes through `date_bin()`,
added in PostgreSQL 14, so the connection test **refuses an older server up
front** with a message naming the version and the required upgrade — verified
against a real `postgres:13` container — rather than letting it fail deep inside a
scan as an opaque "function date_bin(…) does not exist". Three things to know:

- That precise message **reaches the UI verbatim**, under a
  `Connection test failed:` prefix — `_friendly_test_error` surfaces
  `WarehouseCapabilityError` as authored, because tripl wrote it and it carries
  no host, port or driver text. It used to be generalized away, which sent
  operators to the logs for the one sentence that named their problem.
- `classify_time` marks `time`/`timetz` — and any array type — as unsupported, but
  only BigQuery is wired to *act* on that. A PostgreSQL (or ClickHouse) source
  configured with a time-of-day column still fails later, inside a worker, instead
  of at configuration time.
- BigQuery's rejection is not reliably configuration-time either, and the two
  cases it covers do not behave alike. A **`TIME` column** is rejected wherever the
  column's time *kind* is first read, and building a window predicate reads it, so
  a scan preview does catch it — but only when the config carries a lookback
  window: with no `scan_lookback_hours`, `resolve_lookback_window` returns `None`,
  `worker.tasks.scan` then passes `time_column=None`, no predicate is built and the
  kind is never asked for. A **`DATE` column at a sub-day interval** is caught by
  **no** preview, lookback window or not: that rejection lives in
  `_bucket_expression`, which only a `get_time_bucketed_*` call reaches, and
  neither preview half nor the dry run makes one — a preview job carries no
  interval at all (`ScanPreviewJob` has no such column). So it is always the first
  collection that surfaces it, and catching it at save time would take a check
  nothing performs today. Both are raised as `WarehouseCapabilityError`,
  which the worker surfaces **verbatim**, so the message names the column and the
  setting to change rather than reading "Scan failed due to an internal error." on
  every tick.

**[8] ClickHouse `Tuple`/`Map` are shape-enumerated, not value-extractable.**
`classify_complex` recognizes them as complex kinds, and the scan now groups them
by their per-row shape — the sorted key set for a `Map`, the declared field names
for a `Tuple` — instead of calling `JSONAllPaths` on them. That call is the reason
this caveat used to understate the damage: `JSONAllPaths` rejects both families
with `ILLEGAL_TYPE_OF_ARGUMENT`, so a single `Map` or `Tuple` column anywhere in a
source query failed *every* scan and *every* metrics collection for that config,
not just the nested field.

What is still unavailable is a selectable **value** path: `get_json_path_samples`
returns no candidates for these columns, so the UI offers none and
`json_passthrough_paths` stays empty while the field is still marked JSON. Map
leaf access needs a subscript (`` `m`['k'] ``) rather than the dotted member
access the adapter compiles today, which ClickHouse rejects on a Map — that is
a known gap (see the table at the end). (BigQuery `STRUCT`/`RECORD`, which was in
the same position, is now value-extractable — see caveat [5] for its one remaining
exclusion.)

One narrow exception since F23.6: a property typed by hand as a breakdown, drift
field or contract field (`<column>.<path>`) is extracted from these columns too —
a `Map` by one key (`mapContains` plus a `` `m`['k'] `` subscript, so only a
one-segment path), a named `Tuple` element by member access. Scans still discover
no path under them, so nothing offers such a property in the picker unless it is
typed in. This extraction is covered by SQL-string tests only; the conformance
gate does not execute it yet.

Verified by execution: the ClickHouse conformance fixture carries a
`Map(String, String)` and a `Tuple(a Int32, b String)` column alongside its
`JSON` one, and the gate scans, buckets and runs discovery over all three.

**[9] Free-text SQL metrics are dialect-specific by definition.**
A SQL metric runs the user's own query. It is executed through `get_preview_rows`,
so it is bounded by `METRIC_QUERY_ROW_LIMIT` (100,000 rows) *per replay chunk* —
a real bound, but a per-chunk one, and the query is expected to pre-aggregate.
Portability is the author's responsibility: tripl does not translate the SQL
between dialects and does not intend to. What tripl *does* do is run
`lint_dialect_sql` against the selected warehouse's dialect, after the read-only
gate, in two places: the metric preview, and the metric save. A query that
provably cannot resolve on that warehouse — the `date_trunc('day', ts)`
string-first form on BigQuery, for instance — gets an actionable message instead
of a driver error. The lint can only ever reject more, never admit more.

Saving a `sql` metric, or a `fact` metric whose free-text `filter_sql` names a
function the fact table's warehouse does not have, is refused with `422` and the
same message the preview shows. That holds for every door, because the web UI,
the REST API, the CLI and agents all save through the same service. The save
lints only SQL that is new or changed: a new metric, a changed SELECT or filter,
or unchanged SQL pointed at a warehouse of another dialect. A rename, a recolour
or any other edit that leaves the SQL alone still saves a metric stored before
this check existed, and so does a form that resends its unchanged definition.
Collection does not run the lint, so such an older metric can still fail in a
worker with the warehouse's own (sanitised) error until someone edits its SQL.
The lint catches only the functions it knows about; preview a free-text SQL
metric before you save it to catch the rest.

**[10] The synthetic adapter is a fixture, not a warehouse.**
`test_connection` is an honest *local* check — both in-memory tables hold rows —
and never claims a network connection. It recognizes only the scan shapes it can
compute over its fixture and raises `SyntheticCapabilityError` for anything else,
rather than inventing a plausible number. Its dataset is held to **65,000 rows
per table**, checked once when the tables are generated rather than on every scan
(the old per-scan check compared the generators' output against itself and could
not fire), so its sampled paths happen to be exact *for it* — an accident of
size, not a guarantee.

Three things it does *not* do like a warehouse, and each one is a refusal rather
than a guess:

- **A SQL metric is matched by exact statement text**, not by probing for
  fragments. The demo's seeded active-sessions statement (and its pre-`GROUP BY`
  spelling, which existing demos still carry) is computed from the dataset;
  editing that SQL — adding a `WHERE`, dividing by two, reading another table —
  raises the capability error instead of returning the unfiltered series under
  the new query's name.
- **A row filter is read, not ignored.** Both ways a fact filter arrives are
  honoured: `AggregateSpec.filter_sql` on the batched path, and the top-level
  `WHERE` of a `SELECT * FROM (<source>) AS _filtered WHERE …` wrapper on the
  per-metric path. The fact table's *own* `WHERE` is honoured on both paths
  too, so the two paths agree. That includes a `WHERE` inside the wrapped source
  and a `WHERE` inside a single CTE (`WITH x AS (… WHERE …) SELECT * FROM x`).
  A `WHERE` nested anywhere else, such as a subquery in the projection or a
  second CTE, is refused rather than dropped. What it can evaluate is a column
  compared to a string or numeric literal, combined with `AND`/`OR` and
  parentheses, in ClickHouse's *or* PostgreSQL's quoting; a function call, a
  subquery or a timestamp condition is refused. A trailing `ORDER BY`, `LIMIT`,
  `OFFSET` or `FETCH` after the `WHERE` is ignored, as it already is without a
  `WHERE`, because the adapter applies its own ordering and row cap. A trailing
  `GROUP BY`, `HAVING`, `WINDOW`, `QUALIFY` or set operation is refused by name.
- **There is no query timer.** The data source's timeout is accepted and ignored:
  the dataset is built in the constructor and every scan is an in-memory pass over
  at most the row cap.

Its aggregates follow the SQL engines. In particular, `sum` over a bucket whose
rows all have a NULL measure is NULL, not `0`, so the bucket is a gap in the
stored series as it is on ClickHouse, PostgreSQL and BigQuery. The demo used to
store a zero there.

**[11] Anomalies and alerts are warehouse-agnostic.**
They are computed after collection, in Python, from the `MetricValue` rows already
stored in tripl's own database — no adapter is involved. They are therefore at
parity *by construction*, and inherit exactly the correctness of the metric
collection that fed them.

**[12] Cancelling a *job* is cooperative; cancelling a *query* is not always
possible.** Stopping a scan or collection sets its status to `cancelled`; the
worker notices **between chunks** and bails out. It does not reach into a query
that is already in flight. What each warehouse does with the in-flight query:

- **BigQuery** — the adapter calls `job.cancel()` when its own deadline expires,
  and `job_timeout_ms` makes the server abandon the job independently. So a
  timed-out BigQuery query does stop.
- **PostgreSQL** — a server-side `statement_timeout` aborts the query when the
  data source's timeout elapses.
- **ClickHouse** — `send_receive_timeout` bounds the client's wait.

In none of the three does pressing **Stop** in the UI kill a single long-running
query mid-flight; it takes effect at the next chunk boundary.

**[13] PostgreSQL TLS is configurable — and an unset mode is resolved per
host.** `sslmode` used to be hard-coded and `extra_params`
was silently ignored. It is now a typed connection setting (`disable`, `allow`,
`prefer`, `require`, `verify-ca`, `verify-full`) alongside a CA certificate,
client certificate and client private key (PEM content, the key stored encrypted
and never returned by the API) and a `search_path`. Inapplicable combinations are
rejected rather than swallowed: certificate material on `sslmode=disable`, a
verifying mode with no CA, half of a client-certificate pair.

**When you do not choose a mode, a remote host gets `require`** — a server
without TLS is a loud connection failure, not a silent downgrade — **and
localhost gets `prefer`** (dev and Docker servers rarely have a certificate,
and the traffic never leaves the machine). An explicit `prefer` still means
what it always did: TLS if the server offers it, **plaintext if it does not**,
and a stripped connection is then indistinguishable from a healthy one.
`require` encrypts but does not check the certificate; if you need the link to
be *authenticated* as well, choose `verify-full` and supply the CA. Do not read
"we support TLS" as "your connection is verified".

**[14] Databricks: what is bounded, and what is only believed.**

- **Not executed.** Every Databricks statement is checked against a mocked
  driver only (see [proven versus believed](#read-this-first-proven-versus-believed)).
  What a live check must exercise is listed in `test_databricks_adapter.py`'s
  module docstring; `conformance/test_databricks_value_conformance.py` runs the
  value half of it against the shared reference when given credentials
  (`TRIPL_CONF_DBX_HOST`, `TRIPL_CONF_DBX_HTTP_PATH`, `TRIPL_CONF_DBX_TOKEN`).
- **Schema browse** is one `information_schema.columns` query over the source's
  catalog, covering the default schema plus the **schema allowlist**, capped at
  50,000 column rows and 30 seconds. Other catalogs are not browsed; qualify
  them by hand.
- **Scan-time shapes are top-level only**, the trade PostgreSQL makes [6]: a
  `VARIANT` document groups on `json_object_keys(to_json(col))`, a `MAP` on its
  key set, a `STRUCT` on its declared paths. Nested paths are still discovered
  for the picker (from sampled rows [4]) and any of them can be extracted.
- **Nested values:** a `VARIANT` path compiles to `` `col`:['a']['b'] ``; a
  `STRUCT` path to `` `col`.`a`.`b` `` and only for fields the type declares; a
  `MAP` has one level of keys (`try_element_at(col, 'k')`). Fields under an
  `ARRAY<STRUCT<…>>` are listed but cannot be addressed.
- **Timeout:** the data source's timeout is sent as the session's
  `STATEMENT_TIMEOUT` (the warehouse cancels on its own), used as the driver's
  socket timeout, and enforced by the adapter, which cancels the running
  statement from a timer when it runs over.

---

## Setup requirements and permissions

### ClickHouse

| | |
| --- | --- |
| Minimum version | 25.x for the `JSON` type paths (`JSONAllPaths` / `JSONDynamicPaths`); older servers work for non-JSON scans. Verified against **25.8**. |
| Default port | 8123 (HTTP) |
| Credentials | host, port, database, username, password |
| Privileges | `SELECT` on the scanned tables. tripl never writes. |
| Source-specific setting | **JSON path discovery** — `dynamic` (`JSONDynamicPaths`, the default, faster on wide JSON columns) or `all` (`JSONAllPaths`, lists shared-data paths too). Affects the discovery probe only, and only for `JSON` columns: both functions reject a `Map` or `Tuple`, so those are skipped rather than probed (caveat [8]). The scan's own shape expression picks its function from the column's nested family and is unaffected by this setting. |

### PostgreSQL (as a **warehouse**, not tripl's own database)

| | |
| --- | --- |
| Minimum version | **14** — hard requirement, enforced at connection test. Every bucket query uses `date_bin()`. Verified against **18**. |
| Default port | 5432 |
| Credentials | host, port, database, username, password |
| Privileges | `CONNECT` on the database, `USAGE` on the schemas, `SELECT` on the scanned tables. A read-only role is the right choice. |
| Source-specific settings | **SSL mode** (unset → `require` for remote hosts, `prefer` for localhost — see caveat [13]), **CA certificate**, **client certificate**, **client private key** (all PEM *content*, not paths; the key is stored encrypted and never returned), **search path** (comma-separated plain identifiers). |
| Session | tripl pins `timezone=UTC`, `standard_conforming_strings=on`, `default_transaction_read_only=on`, and a `statement_timeout` derived from the source's timeout, on every connection. `standard_conforming_strings` is what makes tripl's own quote-doubling sound — under the legacy `off` a backslash escapes the closing quote and a value ending in one closes the literal early. `default_transaction_read_only` is defence in depth *behind* the read-only role, not a substitute for it: it is `USERSET`, so SQL that can `SET` it off undoes it. |

### Greenplum

| | |
| --- | --- |
| Minimum version | **Greenplum 6** (PostgreSQL 9.4), enforced at connection test. Greenplum 7 (PostgreSQL 12) and the Cloudberry, Greengage and WarehousePG forks connect the same way. Verified against **6.27.1** and **7.1.0**. |
| Default port | 5432 (the coordinator) |
| Credentials, privileges, settings, session | Exactly as PostgreSQL: the same connection settings and the same session pins. |
| Differences from PostgreSQL | Buckets by epoch arithmetic instead of `date_bin` (see the week table above). Greenplum 6's regex engine has no lookbehind, so a regex contract using it is skipped there (per expectation, like any pattern an engine refuses). `gp_toolkit` and Greenplum's other catalog schemas are left out of the schema browser. |

### Amazon Redshift

| | |
| --- | --- |
| Compute | A **Serverless workgroup** or a **provisioned cluster**, over its PostgreSQL-protocol endpoint. |
| Default port | 5439 |
| Credentials | host (the endpoint), port, database, username, password — a database user, not IAM. |
| Privileges | `USAGE` on the schemas, `SELECT` on the scanned tables. **Redshift has no read-only session switch**, so these privileges are the only write barrier: grant nothing more. |
| Source-specific settings | As PostgreSQL: SSL mode, CA / client certificate, search path. |
| Session | tripl sets `timezone` to UTC, `statement_timeout` and the search path with `SET` after connecting (Redshift takes no startup options). Redshift reads a backslash in a string literal as an escape and has no `standard_conforming_strings`, so tripl doubles backslashes as well as quotes in every literal it writes. |
| Differences from PostgreSQL | No `date_bin` (epoch arithmetic instead), no aggregate `FILTER (WHERE …)` (conditions fold into `CASE`), values compared as `varchar(65535)` rather than `text` (which Redshift caps at 256 characters), range contracts compare in `double precision` (Redshift's `numeric` holds 38 digits) behind a guard that admits only numbers it can hold, top-N ties break on Redshift's default byte collation. The schema browser reads `svv_columns`, so external (Spectrum) tables and late-binding views are listed. |
| JSON | **Not supported.** A `SUPER` column is an opaque value: no path discovery, no properties, no Parse as JSON. |

### BigQuery

| | |
| --- | --- |
| Credentials | GCP **project ID** (the host field), a **default dataset** (the database field), and a **service-account JSON key** pasted into the form. |
| IAM roles | `roles/bigquery.jobUser` on the project (to run jobs) and `roles/bigquery.dataViewer` on each dataset you scan. Nothing else — tripl never writes. |
| **Location** | The region or multi-region the datasets live in (`EU`, `US`, `us-east1`, …). Leave empty to let BigQuery infer it. **A job started in the wrong location fails** — this is the single most common BigQuery setup error. |
| **Max billed bytes** | Cost guard, default **100 GiB** per query. BigQuery refuses a query estimated to exceed it. |
| **Dataset allowlist** | Comma-separated datasets the schema browser may list, in addition to the default dataset. Empty means the default dataset only. Up to **19** datasets: a browse covers 20 in total and the default dataset takes one slot — see caveat [1]. |

### Databricks

| | |
| --- | --- |
| Compute | A **SQL warehouse** (serverless, pro or classic) in a Unity Catalog workspace. All-purpose clusters are not supported. |
| Credentials | **Server hostname** (the host field), **catalog** (the database field), the warehouse's **HTTP path**, and either a **personal access token** (or a service principal's token) or a service principal's **OAuth client ID and secret** (OAuth machine-to-machine). The port is always 443. |
| Privileges | `CAN USE` on the SQL warehouse; `USE CATALOG` on the catalog, `USE SCHEMA` on each schema, `SELECT` on the scanned tables. tripl never writes, and its SQL gate refuses every write or DDL statement (`INSERT`, `MERGE`, `COPY INTO`, `CREATE`/`ALTER`/`DROP`, `OPTIMIZE`, `VACUUM`, `SET`, `USE`, …). |
| Source-specific settings | **HTTP path** (required), **Authentication** (`pat` or `oauth_m2m`), **default schema** (empty means `default`), **schema allowlist** (at most 50). |
| Session | tripl sets `TIMEZONE=UTC` and `STATEMENT_TIMEOUT` on every session, turns off cloud fetch and the driver's telemetry, and binds every data or analyst value as a named parameter (`:p0`). |
| Driver | `databricks-sql-connector`, without `pyarrow`: results arrive as rows over the Thrift protocol, and complex values (`STRUCT`, `MAP`, `ARRAY`, `VARIANT`) as JSON text. |
| Outbound guard | With `OUTBOUND_PUBLIC_HOSTS_ONLY`, the host must be a Databricks workspace hostname, because the driver cannot be pinned to the vetted address. See [Security](../run/security.md). |

### Trino

| | |
| --- | --- |
| Compute | A Trino coordinator or a Starburst cluster, over its HTTP(S) endpoint. Verified against **Trino 483**. |
| Default port | 443 (`https`); 8080 for a coordinator without TLS (`http`) |
| Credentials | host, port, **catalog** (the database field), user, and a password for password authentication. A password is only sent over HTTPS. |
| Privileges | Read access to the catalog's tables. tripl never writes, and its SQL gate refuses every write or DDL statement. |
| Source-specific settings | **Scheme** (`https` / `http`), **default schema**, **schema allowlist** (at most 50). |
| Session | tripl sets the session time zone to UTC and `query_max_run_time` to the source's timeout, and the client's request timeout to match. Values are written as single-quoted literals (see [Security](../run/security.md#outbound-requests)); the client retries nothing. |
| Driver | `trino` (the Trino Python client). Every request, result pages included, goes to the vetted address. |

### Amazon Athena

| | |
| --- | --- |
| Compute | Athena engine version 3, in a **workgroup** (`primary` by default). |
| Credentials | the **AWS region** (the host field), the Glue **database**, an **access key ID** (username field) and **secret access key** (password field). |
| Privileges | Athena query execution in the workgroup, Glue read access to the catalog, S3 read access to the data and write access to the query result location. |
| Source-specific settings | **Workgroup**, **query result location** (`s3://…`), **catalog** (default `AwsDataCatalog`), **schema allowlist** (at most 50). |
| Session | Athena has no session time zone setting; its engine runs in UTC. No statement timeout either: tripl stops the query (`StopQueryExecution`) when the source's timeout passes. |
| Driver | `pyathena` over `boto3`, pointed at the region's own Athena endpoint only. Results are read through `GetQueryResults`. |

### Every warehouse

**Timeout (seconds)** applies to all four real source types, BigQuery and
Databricks included, and defaults to **300s**. It bounds the connect handshake and the query itself
(`send_receive_timeout` on ClickHouse, `statement_timeout` on PostgreSQL,
a result deadline plus `job_timeout_ms` on BigQuery, `STATEMENT_TIMEOUT` plus a
client-side cancel on Databricks, `query_max_run_time` on Trino, a client-side
`StopQueryExecution` on Athena). The synthetic source
accepts the setting and ignores it — there is no wall clock to guard over an
in-memory fixture (caveat [10]).

---

## Dialect-correct examples

A wrong example in documentation is how `date_trunc` got into the SQL metric
starter template in the first place. Every expression below was checked against a
real engine — ClickHouse 26.8 and PostgreSQL 18 by execution, BigQuery by its own
ZetaSQL analyzer.

### Scan base query

The scan's base query is a plain `SELECT`. It is dialect-specific only in how you
qualify and quote names.

```sql
-- ClickHouse
SELECT * FROM analytics.events

-- PostgreSQL
SELECT * FROM analytics.events

-- BigQuery  (bare table name resolves in the default dataset)
SELECT * FROM events
-- ...or qualify it explicitly:
SELECT * FROM `my-gcp-project.analytics.events`

-- Databricks  (bare name resolves in the default schema of the source's catalog)
SELECT * FROM events
-- ...or qualify it with catalog.schema.table:
SELECT * FROM main.analytics.events

-- Trino / Athena  (bare name resolves in the default schema; quote with "...")
SELECT * FROM events
-- ...or qualify it with catalog.schema.table:
SELECT * FROM hive.analytics.events
```

### Time buckets in a SQL metric

This is where the dialects genuinely diverge. GoogleSQL's `DATE_TRUNC` takes
`(date_expr, date_part)` — it has **no** `date_trunc(text, timestamp)` form, so
the ClickHouse/PostgreSQL spelling is a hard error on BigQuery
(`A valid date part name is required but found created_at`).

```sql
-- ClickHouse  (and the synthetic demo warehouse)
SELECT toStartOfInterval(created_at, INTERVAL 1 DAY, 'UTC') AS bucket,
       count(DISTINCT user_id) AS value
FROM events
GROUP BY 1
ORDER BY 1

-- PostgreSQL
SELECT date_bin(INTERVAL '1 day', created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00:00') AS bucket,
       count(DISTINCT user_id) AS value
FROM events
GROUP BY 1
ORDER BY 1

-- BigQuery
SELECT TIMESTAMP_TRUNC(created_at, DAY, 'UTC') AS bucket,
       COUNT(DISTINCT user_id) AS value
FROM events
GROUP BY 1
ORDER BY 1

-- Databricks  (the session time zone is UTC, so this truncates in UTC)
SELECT date_trunc('DAY', created_at) AS bucket,
       count(DISTINCT user_id) AS value
FROM events
GROUP BY 1
ORDER BY 1

-- Trino / Athena  (AT TIME ZONE 'UTC': date_trunc truncates in a zoned value's own zone)
SELECT date_trunc('day', created_at AT TIME ZONE 'UTC') AS bucket,
       count(DISTINCT user_id) AS value
FROM events
GROUP BY 1
ORDER BY 1
```

The **New metric** screen renders exactly these, per selected data source, and
re-renders when you switch sources — as long as you have not yet edited the SQL,
in which case your text is never overwritten. Note the absence of SQL comments:
the read-only gate rejects every comment marker that is not inside a string or
quoted-identifier literal. (A value may contain one — `utm_campaign = '#launch'`
passes — but an unterminated literal is scanned as if it were code, so the text
after a stray quote is still rejected.)

Weekly buckets, if you write them by hand, must say Monday explicitly:

```sql
-- ClickHouse
toDateTime(toMonday(created_at, 'UTC'), 'UTC')

-- PostgreSQL
date_bin(INTERVAL '7 days', created_at, TIMESTAMPTZ '1970-01-05 00:00:00+00:00')

-- BigQuery
TIMESTAMP_TRUNC(created_at, WEEK(MONDAY), 'UTC')

-- Databricks  (WEEK truncates to the Monday)
date_trunc('WEEK', created_at)

-- Trino / Athena  (week truncates to the Monday)
date_trunc('week', created_at AT TIME ZONE 'UTC')
```

### Fact tables and measure columns

A fact metric points at a table and a numeric measure column; tripl generates the
aggregate. Nothing dialect-specific is required of you here beyond the base query
above — but the **measure column must be numeric**, and on BigQuery it must not be
`REPEATED`: GoogleSQL cannot cast an `ARRAY` to a single value, nor group by one,
so an array-valued column is rejected when you select it rather than failing in a
worker.

### JSON paths

Configure a nested field as a **dotted leaf path** — the same string on all three
warehouses:

```
payload.user.address.city
```

tripl compiles it per dialect:

| Warehouse | Compiled extraction |
| --- | --- |
| ClickHouse | `` `payload`.`user`.`address`.`city` `` (JSON subcolumn access) |
| PostgreSQL | a `jsonb` path traversal over `payload` |
| BigQuery (`JSON` column) | ``JSON_QUERY(`payload`, '$.user.address.city')`` |
| BigQuery (`STRUCT` column) | `` `payload`.`user`.`address`.`city` `` — dotted field access, and only for paths the schema declares |
| Databricks (`VARIANT` column) | `` `payload`:['user']['address']['city'] `` |
| Databricks (`STRUCT` column) | `` `payload`.`user`.`address`.`city` `` — only for fields the type declares; a `MAP` column takes one key |
| Trino / Athena (`json` column) | `json_extract_scalar("payload", '$["user"]["address"]["city"]')` (bracketed keys match exactly) |

Path parts must be identifier-safe (`[a-zA-Z_][a-zA-Z0-9_]*`). A part that is not
is **rejected, not escaped** — the path is interpolated into SQL, so the allowlist
is a security boundary, not a convenience.

### Time columns

| Warehouse | Use | Do not use |
| --- | --- | --- |
| ClickHouse | `DateTime`, `DateTime64`, `Date`, `Date32` | — |
| PostgreSQL | `timestamptz` (best), `timestamp`, `date` | `time`, `timetz` |
| BigQuery | `TIMESTAMP` (best), `DATETIME`, `DATE` | `TIME`; and **no sub-day interval on a `DATE` column** |
| Databricks | `TIMESTAMP` (best), `TIMESTAMP_NTZ`, `DATE` | **no sub-day interval on a `DATE` column** |
| Snowflake | `TIMESTAMP_TZ` or `TIMESTAMP_LTZ` (best), `TIMESTAMP_NTZ` (read as UTC), `DATE` | **no sub-day interval on a `DATE` column** |
| Trino, Athena | `timestamp with time zone` (best), `timestamp` (read as UTC), `date` | `time`; **no sub-day interval on a `date` column** |

---

## Troubleshooting

### The query timed out

**Symptom:** "query exceeded the *N*s timeout configured for this data source and
was cancelled", or a scan/collection that fails after roughly the source's
timeout.

Narrow the time window, reduce the columns the base query selects, or raise the
data source's **Timeout (seconds)**. A smaller replay chunk does not help if the
statement that timed out is a breakdown's top-N ranking: that one query reads
the whole replay window by design (see
[Top-N breakdowns rank over the whole collection window](#top-n-breakdowns-rank-over-the-whole-collection-window)),
so narrow the replay window instead. On BigQuery the job is cancelled server-side, so it
stops billing; on PostgreSQL `statement_timeout` aborts it. Pressing **Stop** on a
running job takes effect at the next chunk boundary, not mid-query — see
caveat [12].

### BigQuery: "Not found: Dataset … was not found in location …"

The source's **Location** is wrong or unset while the datasets live in a
non-default region. Set it to the region or multi-region the datasets are in
(`EU`, `US`, `us-east1`, …). This is the most common BigQuery misconfiguration.

### BigQuery: the query was refused before it ran

`maximum_bytes_billed` did its job: BigQuery estimated the query would bill more
than the source's **Max billed bytes** (default 100 GiB) and refused it. Either
the base query is scanning far more than you think — check for a missing partition
filter or an accidental cross join — or the scan is legitimately large and the
guard should be raised deliberately.

### BigQuery: permission denied, or autocomplete is missing tables

The service account needs `roles/bigquery.jobUser` on the project and
`roles/bigquery.dataViewer` on every dataset you scan. A dataset the credentials
cannot read is **skipped** during schema browse (logged, not fatal), so missing
tables in autocomplete usually means a missing `dataViewer` grant — or a dataset
that is neither the default nor in the **Dataset allowlist** (caveat [1]).

### BigQuery: "time column … has type TIME" / "cannot be bucketed at '1h'"

Both are deliberate rejections, and both now reach you verbatim in the job's
error message rather than as "failed due to an internal error". `TIME` carries no
date and cannot be windowed at all; a `DATE` column has no time-of-day and cannot
take a sub-day interval. Neither is caught by *saving* the configuration, and they
do not surface at the same moment either. The `TIME` rejection fires wherever the
column's time kind is first read, which a preview does when it builds its window
predicate — so a preview catches it, but only when the config carries a lookback
window; without one the preview never asks for the kind. The "cannot be bucketed"
rejection fires only when the interval is compiled into a bucket expression, and
only a collection compiles one: a preview job carries no interval, so **no
preview catches a `DATE` column at a sub-day interval**, lookback window or not —
the first run after the change is where you will see it. Pick a
`TIMESTAMP`/`DATETIME` column, or a `1d`/`1w` interval.

### PostgreSQL: the connection test names a version requirement

The adapter raises precise, actionable errors — most notably **"PostgreSQL 13.x
is too old for tripl … `date_bin()` … upgrade to 14 or newer"** — and
`_friendly_test_error` shows them verbatim under a `Connection test failed:`
prefix. If your server is older than 14, the message says so; no log-diving
required. Only exceptions tripl did **not** author are generalized, because
those carry host, port and driver text.

### PostgreSQL: TLS is not doing what you think

An unset `sslmode` resolves to `require` for a remote host and `prefer` for
localhost. An explicit `prefer` falls back to **plaintext** without complaining
if the server does not offer TLS, and `require` encrypts without authenticating —
use `verify-full` (plus a CA certificate) to also verify the server. Other
errors you may see are deliberate:

- *"sslmode=disable never negotiates TLS, so … cannot be applied"* — remove the
  certificate material or raise the mode.
- *"sslmode=verify-ca verifies the server certificate but no sslrootcert was
  given"* — supply the CA.
- *"Client certificate authentication needs both sslcert and sslkey"* — supply
  both.
- *"… must be PEM content (a `-----BEGIN…` block), not a file path"* — paste the
  certificate itself; the server has no filesystem you can point at.

### PostgreSQL: buckets look shifted by a few hours

They should not be: tripl pins `timezone=UTC` on the session and renders every
window bound with an explicit `+00:00` offset, and the conformance gate proves
this against a non-UTC server *and* a non-UTC column. If you are comparing tripl's
buckets against a hand-written query, check that *your* query is not being read in
the server's or role's timezone.

### A JSON key exists in the data but is not offered in the picker

Discovery samples 1,000 source rows (caveat [4]). A rare key will often not
appear. You can still type the dotted path in by hand — extraction is not sampled.

---

## Known intentional differences

These are real divergences that tripl does **not** paper over, because papering
over them would mean lying about the data.

### ClickHouse `DateTime64(6)` window literals do *not* hurt index pruning

Recorded here as a **disproven** worry, so nobody spends the afternoon re-deriving
it. Pinning the window bounds to explicit UTC (`parseDateTime64BestEffort(…, 6,
'UTC')`) fixed a real correctness bug — on a `DateTime('Asia/Tokyo')` column the old
offset-less literal matched *zero* of six in-window rows. The obvious follow-up fear
was that comparing a `DateTime` primary key against a `DateTime64(6)` literal would
defeat the primary-key range scan and quietly turn every bounded scan into a full
one.

Measured on a 5M-row `MergeTree ORDER BY ts` (ClickHouse 26.8), with
`EXPLAIN indexes=1`:

| window literal | parts | granules |
|---|---|---|
| old, offset-less string | 1 / 5 | **1 / 611** |
| new, `parseDateTime64BestEffort(…, 6, 'UTC')` | 1 / 5 | **1 / 611** |
| `DateTime64(3)` variant | 1 / 5 | **1 / 611** |

Identical, and both read the same number of rows. ClickHouse coerces the literal to
the column's type *before* the index is consulted, so the primary key is used exactly
as before. No change was made, because there was nothing to fix.

### ClickHouse cannot discover a key whose only value is JSON `null`

For the same document, the warehouses disagree:

- **PostgreSQL** (recursive `jsonb_each` walk) reports it: `{"a": null}` yields
  path `a`.
- **The local reference implementation** (`tripl.json_paths.flatten_json_paths`)
  reports it.
- **ClickHouse** does **not**. Its `JSON` type never materializes a null-valued
  dynamic subcolumn, so `JSONAllPaths` never reports the path and the key is
  invisible to discovery.

**Consequence:** a field that is present-but-null in every sampled row is
discoverable on PostgreSQL and not on ClickHouse — and a
`required_null_violation` contract cannot even be *configured* on ClickHouse for
such a field. The conformance gate does not hide this: it asserts that null-only
paths are the **only** paths ClickHouse is missing (any other missing path fails
the build), and it will also fail if ClickHouse ever starts reporting them, at
which point the exclusion comes out.

### BigQuery groups arrays by their JSON text

GoogleSQL flatly refuses `GROUP BY <array>` ("Grouping by expressions of type
ARRAY is not allowed"), and refuses a constant array just as hard ("Cannot GROUP
BY literal values") — both verified against ZetaSQL. ClickHouse *can* group by an
`Array(String)` and hands the group key back as a list. So on BigQuery every
array-valued grouped column — a nested column's leaf-path set, and a `REPEATED`
scalar column — is grouped by its `TO_JSON_STRING` rendering, which is a scalar
and groups fine, and decoded back into a list on the way out.

The observable result is deliberately identical on both warehouses: one group per
distinct array value (order-sensitive on both), surfaced to callers as a list.
The JSON text is an implementation detail of the SQL, not of the row contract.

### Regex contracts use four different regex engines

`regex_violation` compiles the stored pattern with PostgreSQL's `~` (POSIX ARE),
ClickHouse's `match()` (RE2), BigQuery's `REGEXP_CONTAINS` (RE2), Databricks'
`RLIKE` (Java `java.util.regex`), Trino's and Athena's `regexp_like` (Joni, a
Java-syntax engine), and Python's `re.search` in the fallback. All
six are **unanchored partial matches**, and all five agree on ordinary patterns — literals, character classes, anchors, `|`,
quantifiers, `\d` / `\w` / `\s`. They do not agree on everything, and tripl does
not pretend otherwise. Two divergences worth knowing, and they point in opposite
directions:

- `\b` is a word boundary in Python and RE2 and a *backspace* in POSIX ARE, so a
  `\b` pattern matches nothing on PostgreSQL.
- Lookaround (`(?=`, `(?!`, `(?<=`, `(?<!`) and backreferences are valid in
  PostgreSQL's ARE and in Python but are rejected by **RE2**, so those patterns
  fail on ClickHouse and BigQuery instead. Java and Joni accept them.
- Python's named group `(?P<name>…)` is refused by Java (which spells it
  `(?<name>…)`), by Joni and by POSIX ARE, so it fails on Databricks, Trino,
  Athena and PostgreSQL.

A pattern the engine refuses costs exactly that one expectation. Before the
statement is built, tripl offers the pattern to the engine itself (`SELECT
match('', …)` on ClickHouse, `SELECT REGEXP_CONTAINS('', …)` on BigQuery,
`SELECT '' RLIKE :p0` on Databricks, `SELECT regexp_like('', '…')` on Trino and
Athena); a
refusal drops that expectation, and every other contract in the scan is still
evaluated. The engine is asked rather than screened against a "portable subset",
because a static screen would have to reject the lookahead a PostgreSQL-only
project is entitled to write.

A refusal is counted, not only logged. The worker logs a warning naming the
column (`Field contract skipped: … cannot compile the pattern …`), and the
collection's run summary reports every expectation dropped this way as
`contract_expectations_skipped`, next to `contract_violations_detected` and
`contract_checks_failed`. The same counter covers the other single-expectation
skips: an enum, regex or range contract on a BigQuery `REPEATED` column, a range
bound that is not a finite number, and, on BigQuery and the sampled fallback, a
contract on a column missing from the result. `contract_checks_failed` counts something else: event types whose whole
contract check raised. A contract that silently stops being evaluated looks
exactly like a contract that is being met, so if a contract you rely on stops
producing drifts, check `contract_expectations_skipped` in the run summary
before concluding the data is
clean. Saving the pattern does not warn you either: the save-time check is a typo
screen against Python's own `re` and deliberately not a portability guarantee, so
a pattern can save here and still be one an engine refuses.

### PostgreSQL range contracts compare exactly

A range contract compares each value against its bounds. The Python fallback,
ClickHouse (`toFloat64OrNull`) and BigQuery (`SAFE_CAST(… AS FLOAT64)`) parse
the value as a 64-bit float and compare the float. PostgreSQL compares in exact
`numeric`. It cannot use `double precision`, because PostgreSQL's float parser
raises an error on overflow (`1e400`) and on underflow to zero (`1e-400`), and
one such row would fail every contract in the scan.

Exact and rounded comparisons give the same verdict except in three cases:

- **A value within one float rounding step of a bound.** `9007199254740993`
  against a maximum of `9007199254740992.0` is out of range on PostgreSQL. The
  other engines round both numbers to the same float, so the value is in range.
- **A value beyond float range.** `-1e-400` against a minimum of `0.0` is out of
  range on PostgreSQL. The fallback reads it as `-0.0`, which is in range.
- **A very long number.** PostgreSQL treats a number with more than 510 integer
  digits or more than 1275 fractional digits as unparseable, so the row is out of
  range. That is well beyond anything a float can express, but the fallback and
  ClickHouse still parse it.

Each case needs a value right at a bound or an extreme number. If a contract on PostgreSQL
reports a violation that the same data does not produce on another warehouse,
check whether the sample value falls into one of these cases.

### BigQuery `DATETIME` is zone-less

A `DATETIME` column is a wall clock with no zone. tripl renders its window
literals as `DATETIME '…'` (no offset — BigQuery rejects one) spelling the UTC
wall clock. If your `DATETIME` column holds local time rather than UTC, tripl's
windows will not mean what you expect. Use `TIMESTAMP` if you can.

The same asymmetry exists on the way *out*, and the adapter closes it: the driver
decodes a `TIMESTAMP` bucket to an aware `datetime`, a `DATETIME` bucket to a
naive one and a `DATE` bucket to a `date`, so the bucket column's Python type
would otherwise depend on a column type the caller never sees. Every bucketed
rowset is normalized to an aware UTC `datetime` before it leaves the adapter — a
`DATE` bucket becoming that date at 00:00 UTC — because the readers compare it
against an aware window bound and persist it into a `timestamptz`. The metric
writers normalize the bucket again on their own side
(`core.bucketing.stored_bucket`), so a naive bucket from any adapter is stored as
UTC rather than in the database session's timezone, and tripl pins its own
application database sessions to `TimeZone=UTC` as well.

**Rows stored before this normalization are not rewritten.** Before it, a
`DATETIME` or `DATE` bucket reached the application database naive, and
PostgreSQL stored it in the *session* timezone. If that timezone was UTC — the
default for a PostgreSQL container initialised without a `TZ`, which is how
`compose.yaml` starts it — old and new rows are the same instants and there is
nothing to do. If it was not, a BigQuery scan config whose time column is
`DATETIME` or `DATE` has its older event, breakdown, coverage and distribution
buckets shifted by the UTC offset. Fact and SQL metric values are not affected:
their writer always stamped a naive bucket as UTC.
The next collection stores new buckets at the right instants, so rows at the
edges of an overlapping window can exist twice for one logical bucket. To check,
run `SHOW TimeZone;` against the application database with the role tripl
connects as, and inspect a few `event_metrics.bucket` values for such a config:
on an hourly or daily grid they should fall on whole UTC hours. To repair
shifted rows, re-collect the config's whole history with a metrics replay whose
window starts before its first stored bucket. Collection deletes the rows inside its
window before writing, so that removes the shifted rows. `TIMESTAMP` columns were
never affected.

---

## What was broken and is now fixed

For the record, so the matrix above is not read as static. Every item below was a
**real defect in shipped code**, not a hypothetical:

- **BigQuery emitted a function that does not exist.** Every generated bucket
  query used `TIMESTAMP_BIN`. GoogleSQL has no such function ("Function not
  found: TIMESTAMP_BIN"). *Every* event, fact, ratio and breakdown metric that
  generated bucket SQL was rejected by BigQuery before returning a single row. Now
  `TIMESTAMP_BUCKET` / `DATETIME_BUCKET` / `DATE_BUCKET` chosen by the column's
  declared time type, and `*_TRUNC(…, WEEK(MONDAY))` for weeks. **Proven against
  ZetaSQL.**
- **BigQuery scans over JSON/STRUCT/REPEATED columns grouped by an ARRAY**, which
  GoogleSQL rejects outright — so those scans had *never* worked. Now grouped by a
  scalar JSON rendering and decoded back. **Proven against ZetaSQL.**
- **PostgreSQL JSON never activated at all.** The complex-type classifier matched
  `"JSON"` case-sensitively and psycopg reports `json` / `jsonb` in lowercase, so
  every PostgreSQL JSON column was classified as a plain scalar. JSON preview,
  discovery and path extraction were all dead code on PostgreSQL. Classification
  is now case-insensitive across all three dialects.
- **Week buckets started on Thursday** on PostgreSQL, BigQuery *and* the frontend
  — a seven-day bin anchored at the epoch, and 1970-01-01 was a Thursday. All are
  Monday now, from a single documented origin. ClickHouse was already correct.
- **Field contracts were evaluated over a 50,000-row sample** on BigQuery and
  PostgreSQL, in Python, while ClickHouse evaluated them warehouse-side over the
  full window — so a violation first occurring at row 50,001 was *not detected at
  all*, and the reported `bad_rate` described the sample rather than the data.
  Both now evaluate **warehouse-side over the full configured window**. (The
  50,000 figure survives only as a cap on how many violation *rows* come back.)
- **BigQuery had no query timeout whatsoever** and no cost guard. It now has both,
  plus best-effort job cancellation and a bounded multi-dataset schema browse with
  `dataset.table` qualification.
- **PostgreSQL's `sslmode` was hard-coded to `prefer`** and stored `extra_params`
  were silently ignored. TLS is now a typed setting with CA and client
  certificates, alongside `search_path` — and an unset mode now resolves
  host-aware: `require` for remote hosts, `prefer` only for localhost (caveat
  [13]).
- **The SQL starter template emitted one `date_trunc` form for every warehouse.**
  GoogleSQL has no `date_trunc(text, timestamp)`, so BigQuery users were handed a
  starter query that could not run. Templates are per-dialect now, and a
  pre-flight lint catches the same mistake in hand-written SQL at preview time.
- **Adapter tests asserted SQL strings against fake clients.** A test like that
  passes whether or not the SQL is valid — which is precisely how `TIMESTAMP_BIN`
  and `GROUP BY <array>` shipped and stayed green for so long. CI now **executes**
  the generated SQL against real PostgreSQL and ClickHouse containers and
  **analyzes** it with real ZetaSQL for BigQuery. That coverage since grew from
  single adapter calls to the whole pipeline: a fourth conformance gate runs a
  real scan → event generation → replay → event, fact, ratio and batched
  metrics → drift and anomaly recalculation on both executing warehouses,
  compares every series against the pure-Python reference, and requires
  PostgreSQL and ClickHouse to agree with each other. BigQuery analyzes that same
  pipeline on every PR and executes it against the pure-Python reference in the
  credentialed release gate.
- **The data-source *edit* dialog showed ClickHouse/PostgreSQL fields for a
  BigQuery source.** The create and edit dialogs now share one per-warehouse
  field set, so a BigQuery source is edited with its project ID, default dataset
  and service-account key — and stored secrets are never prefilled into the
  form, only sent when actually retyped.

## What is still open

| Gap |
| --- |
| ClickHouse `Tuple`/`Map` columns are shape-enumerated but have **no nested value extractor**: no selectable path in the UI (caveat [8]) |
| A fact-metric breakdown group whose aggregate is all-`NULL` crashes the collector with a `TypeError` instead of being recorded as absent |

The rest of what this table used to list has landed: scan/replay, event
generation, fact metrics and drift now execute against real warehouses in the
pipeline gate; an unset PostgreSQL `sslmode` resolves to `require` for remote
hosts (caveat [13]); the BigQuery **edit** dialog shows BigQuery fields; and the
ClickHouse null-leaf divergence is pinned by the conformance gate as a
[documented intentional difference](#clickhouse-cannot-discover-a-key-whose-only-value-is-json-null)
rather than silently accepted. Details in
[What was broken and is now fixed](#what-was-broken-and-is-now-fixed).

---

## Adding a warehouse, or changing one

1. Implement every abstract method on `BaseAdapter`. There are no optional ones —
   an adapter that cannot do breakdowns is not a warehouse tripl supports.
2. Do **not** inherit `validate_field_contracts` or `get_json_path_samples` and
   call it done. Both base implementations are *bounded fallbacks*: the first
   evaluates 50,000 rows in Python instead of the full window warehouse-side, and
   shipping with it is what caveat [4]'s sharper predecessor described.
3. Make `floor_to_bucket` the test oracle. For every interval code, assert your
   generated SQL produces the same bucket the reference implementation does — in
   particular that your weeks start on **Monday** and your sub-week bins are
   epoch-anchored.
4. Render window bounds with an explicit UTC offset and pin the session timezone
   to UTC. Do not rely on the server being configured correctly.
5. **Add an executable conformance gate**, in `backend/src/tripl/tests/conformance/`.
   `dataset.py` is deliberately warehouse-agnostic — reuse it. A test that asserts
   a SQL *string* against a fake client proves nothing: it passes whether or not
   the SQL is valid, and that is not a hypothetical failure mode here, it is the
   documented history of this codebase.
6. Add a row to the matrix on this page, and say how it was verified. If a path is
   bounded, say so, and file the issue that will unbound it. A capability matrix
   that overstates support is worse than no matrix — that is the failure this epic
   exists to correct.
