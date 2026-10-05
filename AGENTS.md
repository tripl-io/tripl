# AGENTS.md

_Last updated: 2026-07-15._

## What This Repo Is

`tripl` is an analytics tracking-plan and monitoring service.

Use it to:
- manage projects and tracking plans;
- define event types, fields, relations, meta fields, and reusable variables;
- store concrete catalog events with lifecycle, review, ownership, media, and
  change history;
- connect external analytics DBs — ClickHouse, BigQuery, or PostgreSQL;
- run scan jobs that infer events and variables from real data;
- collect time-bucketed event and user-defined business metrics;
- detect anomalies, schema/distribution/value drift, and release regressions;
- review plan changes on branches and reconcile the plan with live data;
- route alerts to chat, email, webhook, and issue-tracker destinations.

An optional local [PLAN.md](PLAN.md) contains the internal product plan and is
intentionally gitignored. Public product/architecture documentation lives under
[website/docs](website/docs). This file is the fast navigation map for agents
working in the codebase.

## Agent Workflow

If `mem0` MCP is available in the current session, use it for every task.
- search or list relevant memories before making assumptions, especially for ongoing work, prior decisions, and user preferences;
- save durable decisions, preferences, and important implementation findings to `mem0` when they are likely to matter in future sessions;
- do not start a separate memory service; this project uses the configured `mem0` MCP.

## Current Product Scope

Already implemented in code:
- session auth, owner/editor/viewer RBAC, and scoped API keys;
- event catalog CRUD, lifecycle, bulk triage, owners, history, photos/specs, and
  comments;
- plan branches, review policy, conflicts, merge, revisions, and optional Jira
  implementation tickets;
- typed variables with documented values, source bindings, per-event overrides,
  drift review, and scan exclusion;
- ClickHouse, BigQuery, and PostgreSQL data sources and scan configs;
- async scan pipeline via Celery + RabbitMQ;
- auto-generated events/variables from cardinality and JSON-path analysis;
- event metrics plus a SQL/fact/event-composition metrics catalog;
- anomaly detection for project total, event type, event, and metric scopes;
- schema, distribution, variable-value, and app-version regression detection;
- reconciliation, coverage, monitoring, search/AI, and audit surfaces;
- alerting with eight destination types, rules, simulation, inbox, retries,
  delivery history, and message templating;
- workspace/project/instance settings and production hardening.

Not a safe assumption unless you verify:
- import/export;
- automatic rollback of a merged branch;
- any local analytics warehouse container.

## Stack And Runtime

Backend:
- Python `3.14`
- `uv`
- FastAPI
- SQLAlchemy async + `asyncpg`
- Alembic
- PostgreSQL
- Celery `5.x`
- RabbitMQ
- `clickhouse-connect`
- `statsmodels` for anomaly logic

Frontend:
- `bun` (package manager, scripts and runtime; `bun.lock`)
- React `19`
- TypeScript `7` for type checking (`tsc`); TypeScript 6 stays installed as
  `typescript` for tools that load its API (see CONTRIBUTING.md)
- Vite `8`
- Tailwind CSS `4`
- Radix UI primitives
- TanStack Query
- Recharts

Local dev runtime in [compose.dev.yaml](compose.dev.yaml) (prod runs the
published image via [compose.yaml](compose.yaml); see [website/docs/run/release.md](website/docs/run/release.md)):
- `postgres`
- `rabbitmq`
- `api`
- `celery-worker`
- `celery-beat`
- `frontend`

Important runtime facts:
- Warehouses (ClickHouse, BigQuery, PostgreSQL) are external. The repo does not run them in Compose.
- `api` runs `alembic upgrade head` before `uvicorn`.
- Celery beat schedules event/catalog metric due-checks every 5 minutes (300s),
  implementation-ticket sync every 5 minutes, and stranded embedding recovery
  every 15 minutes.
- API health endpoint is `GET /health`.
- CORS is open in dev app setup.

### Python 3.14 syntax — do not report these as errors

The backend targets Python 3.14 (`requires-python = ">=3.14"`, ruff
`target-version = "py314"`), so it uses syntax that older interpreters reject.
Most notably [PEP 758](https://peps.python.org/pep-0758/) lets an `except`
clause list several exception types **without parentheses**:

```python
except ValueError, TypeError:  # valid on 3.14; used ~21 times in this repo
```

Any interpreter older than 3.14 reports `SyntaxError: multiple exception types
must be parenthesized` for every one of those lines. That is the wrong
interpreter, not a defect — it has twice been written up as a fabricated "the
backend cannot boot" finding. Run Python through `uv run python` (or
`backend/.venv/bin/python`), never a system `python3`, and never report a
syntax or type error you have not reproduced with the pinned interpreter.

## Environment Variables

Primary backend settings are in [backend/src/tripl/config.py](backend/src/tripl/config.py).

Connectivity:
- `DATABASE_URL`, `SYNC_DATABASE_URL`, `RABBITMQ_URL`, `REDIS_URL`

Identity and secrets:
- `ENCRYPTION_KEY` — Fernet key for at-rest secrets. **Required** in non-debug mode.
- `SECRET_KEY` — session-cookie signing key. **Required** in non-debug mode.
- `SESSION_COOKIE_NAME`, `SESSION_TTL_HOURS`, `SESSION_COOKIE_SECURE`.
- `APP_BASE_URL` — used for alert links and as the default CORS origin.

Edge / hardening:
- `CORS_ALLOW_ORIGINS` — comma-separated origins. Empty + DEBUG=true → `*`; empty + DEBUG=false derives from `APP_BASE_URL`, else denies all.
- `SECURITY_HEADERS_ENABLED`, `HSTS_ENABLED`, `HSTS_MAX_AGE_SECONDS`, `CONTENT_SECURITY_POLICY`.
- `RATE_LIMIT_ENABLED`, `RATE_LIMIT_LOGIN_PER_MINUTE`, `RATE_LIMIT_REGISTER_PER_HOUR`.

Observability:
- `LOG_LEVEL`, `LOG_JSON`, `REQUEST_ID_HEADER`.

Frontend:
- `VITE_API_URL`

Practical notes:
- `SYNC_DATABASE_URL` is used by Celery tasks and other sync SQLAlchemy code paths.
- The app refuses to start in non-debug mode with an empty/invalid `ENCRYPTION_KEY`, no resolvable CORS origin, or `SESSION_COOKIE_SECURE=false`. See `Settings.assert_production_ready()`.
- Keep `.env.example`, Compose env, and app settings synchronized.

## Repo Layout

Top level:
- [PLAN.md](PLAN.md): optional, gitignored internal product notes.
- [README.md](README.md): quick start and user-facing overview.
- [website/docs](website/docs): public product, operations, API, and architecture
  documentation.
- [compose.yaml](compose.yaml): production stack (published image); [compose.dev.yaml](compose.dev.yaml): local dev topology.
- [backend](backend): Python service. Distribution `tripl-server`, import package
  `tripl` — the names differ so the PyPI name `tripl` can be the operator CLI
  below; the import package is unchanged and stays `tripl`.
- [frontend](frontend): React app.
- [cli](cli): the `tripl` operator CLI (import package `tripl_cli`) — the
  read-only `doctor` / `status` / `watch` diagnostics **and the mutating
  `scans run|cancel` / `drifts dismiss` verbs**, plus the **shared request
  layer** (`tripl_cli/api` and the async `TriplClient`) that `mcp-server`
  imports. Apache-2.0, `httpx` only, no backend imports.
- [mcp-server](mcp-server): the `tripl-mcp` MCP server (import package
  `tripl_mcp`). Depends on the `tripl` distribution in `cli/` for its HTTP
  client; there is exactly one `TriplClient` in the repo.

Backend entrypoints:
- [backend/src/tripl/main.py](backend/src/tripl/main.py): FastAPI app, middleware stack, lifespan, and `/health`.
- [backend/src/tripl/extensions.py](backend/src/tripl/extensions.py): extension hooks (routers, access gates, lifecycle, audit, worker). Community bundles no extension: single sign-on per organization, SCIM, the organization-wide audit log (`/audit`), audit export and the audit webhook live in the private Enterprise package, which reaches the core only through them. Community still writes every audit row (`services/audit_service.py`) and serves a project's audit history from `api/v1/project_audit.py` ([extension points](website/docs/develop/extension-points.md)).
- [backend/src/tripl/api/v1/router.py](backend/src/tripl/api/v1/router.py): all API router registration.
- [backend/src/tripl/worker/celery_app.py](backend/src/tripl/worker/celery_app.py): Celery app and beat schedule.

Backend layers:
- `backend/src/tripl/models`: SQLAlchemy models.
- `backend/src/tripl/schemas`: Pydantic request/response models.
- `backend/src/tripl/services`: business logic used by routers.
- `backend/src/tripl/api/v1`: thin HTTP layer.
- `backend/src/tripl/middleware`: request-id, security headers, rate limiting.
- `backend/src/tripl/crypto.py`: Fernet-based at-rest encryption (one source of truth for all callers).
- `backend/src/tripl/logging_config.py`: log handler/formatter wiring.
- `backend/src/tripl/worker/tasks`: async task entrypoints.
- `backend/src/tripl/core/analyzers`: scan/anomaly analysis logic.
- `backend/src/tripl/core/adapters`: analytics DB (warehouse) adapters — ClickHouse, BigQuery, PostgreSQL.
- `backend/src/tripl/tests`: backend tests.

Frontend layers:
- `frontend/src/App.tsx`: route table.
- `frontend/src/extensions/`: frontend extension registry (routes, settings sections, sign-in panels, shell gates). Community registers no frontend extension; `extensions/teasers.ts` shows Enterprise-only features (single sign-on, SCIM, the organization-wide audit log, the audit webhook) as tagged teasers ([extension points](website/docs/develop/extension-points.md)).
- `frontend/src/pages`: screen-level UI.
- `frontend/src/api`: typed HTTP client wrappers.
- `frontend/src/components`: layout and shared UI.
- `frontend/src/types/index.ts`: frontend domain types.
- `frontend/src/**/*.test.*`: Vitest coverage.
- `frontend/e2e/*.spec.ts`: Playwright end-to-end tests. **Every new
  user-facing feature ships with one** (see CONTRIBUTING.md, "End-to-end
  tests"); run them in CI, not on this host.

CLI layers (`cli/src/tripl_cli`):
- `client.py`: the shared async `TriplClient`. **Also imported by `mcp-server`** —
  every change here needs both test suites green.
- `api/`: the shared REST **request layer** — `ApiRequest` values, `send()`, and
  one home per path template. **Both `tripl_cli` and `tripl_mcp` build every
  request only from here**, and `cli/tests/test_contract.py` enforces it: no
  REST path literal and no `ApiRequest(...)` may appear outside this package,
  and nothing outside it may call an HTTP method on a client. Consumer-neutral
  by rule — no `argparse`, no `mcp`, and it never prints.
- `config.py`: per-field flag > env > file resolution with provenance.
- `cli.py` / `commands/`: argparse entry point; one module per subcommand.
  `commands/_write.py` holds the write-safety rules the mutating verbs share
  (`--dry-run`, the confirmation, "never prompt in a pipeline").
- `runner.py`: the only `asyncio.run`, one connection pool per invocation.
- `api/`: the shared request layer — every REST path and request body lives
  here and nowhere else, as frozen `ApiRequest` values. `tripl-mcp` imports it
  too, which is why it holds facts about the API and never a consumer's policy.
- `model.py`, `report.py`, `render.py`: the snapshot dataclasses, the `--json`
  contract ("if a key is not built here it does not exist"), and the ASCII
  output. At the package root rather than under `diagnostics/` because they
  serve every command, including the verdict-free ones.
- `diagnostics/`: the verdict layers only. `collect.py` is async/impure and the
  only thing that speaks HTTP (every failure becomes a `Fetched`, never an
  exception — except `raise_selection_failure` / `read_or_raise`, which the
  verdict-free commands use to opt back out); `checks.py` and `scan_checks.py`
  are pure, synchronous, total functions of a `Snapshot`; `endpoints.py`
  declares which paths each command reads. A contract test pins that closed set.
- `watch/`: the follow loop — `collect.py` polls, `diff.py` is the pure
  snapshot-to-events function, `render.py` is its JSON Lines and ASCII output.
- User-facing reference: [website/docs/run/cli.md](website/docs/run/cli.md).
  `scan_checks.py` mirrors the backoff constants in
  `backend/src/tripl/worker/tasks/metrics/schedule.py` — change one, change both.

## Domain Model Cheat Sheet

Core planning entities:
- `OrganizationGroup`, `OrganizationGroupMember` (F20, `models/organization_group.py`):
  named sets of an org's members, `/api/v1/orgs/{org}/groups`. Membership in the
  org is enforced in `services/org_group_service.py`; `org_service.remove_member`
  drops the user's groups. Consumers (F24 sharing, owners, alert routing) resolve
  groups with `org_group_service.group_member_ids`; SCIM (Enterprise) maps a group to the admin role through `on_group_change`.
- `Organization`, `OrganizationMember` (F20): the tenant above projects, with
  org roles `owner` | `admin` | `member`. Projects, data
  sources, API keys and invitations carry a NOT NULL `organization_id`; every row
  is in the default organization (`DEFAULT_ORG_ID` in `models/organization.py`).
  Org roles and project roles are the only permission source (the old
  instance role, `users.role`, is dropped), and `app_settings` reads must filter `organization_id IS NULL` (operator scope).
  Org context (F20 PR2): every authenticated request acts in one organization,
  bound in `middleware/org_context.py` by `api/deps.py` (`get_current_user` /
  `_resolve_api_key_user` via `services/org_resolution.py`) BEFORE any slug is
  resolved. Order: API key's org (a differing path org is 404); else the path
  org from `/api/v1/orgs/{org}/...` (membership required, else 404); else
  `self_hosted` -> default org, `hosted` -> the user's only org or 400
  "Organization required". `/api/v1/auth/*` resolves none. Resolve projects by
  slug ONLY through `services/project_lookup.py` (`resolve_project`,
  `resolve_project_id`, `project_slug_clause` inside a join); they raise
  `OrgContextMissing` when no org is bound, and `tests/test_project_slug_guard.py`
  fails on any other `Project.slug` comparison. Workers resolve by id; scripts
  wrap slug lookups in `bound_org(...)`. `OrgPathRewriteMiddleware` rewrites only
  `ORG_REWRITE_PREFIXES` (projects, activity, audit, data-sources, users, me) and
  fences the contextvar per request; tests get the default org bound by an
  autouse fixture (opt out with `@pytest.mark.no_default_org`).
- `Project`: tracking-plan namespace.
- `ProjectMember`: a user's membership of one project (`editor` | `viewer`).
  Non-members get 404 on every `/projects/{slug}/...` route and never see the
  project in a list or feed; owners and admins of the project's organization
  need no row (they are project role `owner`). Rules live in
  `services/project_access.py`; the gate is `require_project_membership` in
  `api/deps.py`, mounted in `api/v1/router.py`'s `protected_dependencies`.
- `EventType`: schema bucket like page view or click.
- `FieldDefinition`: typed field under an event type.
- `EventTypeRelation`: relation between event types via fields.
- `MetaFieldDefinition`: project-level metadata schema.
- `Variable`: typed `${placeholder}` with documented values, warehouse bindings,
  per-event overrides, observed contexts, and scan-exclusion state.
- `PlanBranch`, `PlanBranchApproval`, `PlanBranchReviewer`,
  `PlanBranchComment`, `PlanBranchMergeResolution`, `PlanRevision`: reviewable
  plan-change workflow.

Catalog entities:
- `Event`: concrete expected event instance in the plan.
- `EventFieldValue`: field value attached to an event.
- `EventMetaValue`: meta value attached to an event.
- `EventTag`: freeform event tag.
- `EventChange`: field-level event history.
- `EventPhoto`, `EventPhotoComment`: images/Figma specs and threaded discussion.

Analytics and monitoring entities:
- `DataSource`: external analytics DB connection — ClickHouse, BigQuery, or PostgreSQL.
- `ScanConfig`: saved scan definition. Important fields include `base_query`,
  event/time/name mapping, JSON paths, grouping rules, breakdown/drift columns,
  row/lookback/replay limits, app-version/platform roles, and interval.
- `ScanJob`: async execution record for a scan config.
- `EventMetric`, `EventMetricBreakdown`: aggregated event-count buckets.
- `FactTable`: reusable safe source query and named filters for fact metrics.
- `MetricDefinition`, `MetricValue`, `MetricValueBreakdown`: the project-wide
  SQL/fact/event-composition metrics catalog and collected series.
- `MetricAnomaly`, `MetricBreakdownAnomaly`: persisted anomaly buckets.
- `SchemaDrift`, `DistributionDrift`, `VariableValueDrift`,
  `ReleaseRegression`: non-volume detection records.
- `ProjectAnomalySettings`: anomaly detector thresholds and scope toggles.

Alerting entities:
- `AlertDestination`: Slack, Telegram, webhook, email, Jira, Linear, PagerDuty,
  or Microsoft Teams channel config.
- `AlertRule`: filters, thresholds, cooldown, include/exclude scope, and message templates.
- `AlertRuleState`: cooldown/state tracking.
- `AlertDelivery`: one queued/sent/failed delivery attempt.
- `AlertDeliveryItem`: matched anomaly items included in a delivery.
- `ProjectTrackerConfig`, `ImplementationTicket`: separate Jira automation for
  branch-to-implementation workflow.

## API Map

Base prefix: `/api/v1`

Routers currently registered:
- `/auth`, `/users`, `/me/api-keys`, `/settings`
- `/activity`
- `/projects`
- `/projects/{slug}/audit` (`/actions`, `/{entry_id}`; org owner or admin,
  browser session only): the project's audit history
- `/projects/{slug}/members` (list: any member; add/re-role/remove: instance
  owner or the project's creator, browser session only)
- `/projects/{slug}/event-types`
- `/projects/{slug}/event-types/{event_type_id}/fields`
- `/projects/{slug}/relations`
- `/projects/{slug}/meta-fields`
- `/projects/{slug}/variables`
- `/projects/{slug}/events`
- `/data-sources`
- `/projects/{slug}/scans`
- `/projects/{slug}/search`
- `/projects/{slug}/metrics` (catalog plus series)
- `/projects/{slug}/fact-tables`
- `/projects/{slug}/branches`, `/projects/{slug}/revisions`
- `/projects/{slug}/reconciliation`
- `/projects/{slug}/anomaly-settings`
- `/projects/{slug}/alert-destinations`
- `/projects/{slug}/alert-deliveries`
- `/projects/{slug}/annotations`, `/projects/{slug}/tracker-config`
- event-volume metric routes under project, event, and event-type paths

Useful endpoint groups:
- Events: list/filter/create/update/delete, bulk create/update/delete,
  reorder/move, tags, history, photos/specs/comments.
- Variables: CRUD, bulk update/delete, observed contexts, per-event overrides,
  drift list/actions.
- Data sources: CRUD, connection test, stats, and schema browse.
- Scans: CRUD, async preview, run/cancel, groups, replay, version/platform and
  monitoring insight endpoints, job history.
- Metrics:
  - `GET /projects/{slug}/events-metrics`
  - `POST /projects/{slug}/events/window-metrics`
  - `GET /projects/{slug}/metrics/total`
  - `GET /projects/{slug}/events/{event_id}/metrics`
  - `GET /projects/{slug}/event-types/{event_type_id}/metrics`
  - `GET /projects/{slug}/anomalies/signals`
- Catalog metrics/fact tables: CRUD, preview, collect, series, breakdowns,
  versions, reorder/bulk status.
- Branches: lifecycle, reviewers, comments, diff/conflicts/resolutions/merge;
  revisions snapshot/list/diff.
- Reconciliation: shadow/dead events and coverage.
- Health score (F15, main plan only, fixed weights in
  `services/health_weights.py`):
  - `GET /projects/{slug}/health?trend_days=30` (project score, worst five, trend)
  - `GET /projects/{slug}/health/events?ids=...` (batch, 1..150 ids)
  - `GET /projects/{slug}/health/event-types`
  - `GET /projects/{slug}/events/{event_id}/health`
  - `GET /projects/{slug}/events?order_by=health` (least healthy first; 400 on a branch)
- Alerting:
  - destinations CRUD
  - rules CRUD nested under a destination
  - rule simulation, monitor mute/unmute
  - deliveries list/detail/retry and Inbox actions

If you need exact request/response shapes, open the corresponding file in `backend/src/tripl/schemas` before digging into services.

## Frontend Route Map

Defined in [frontend/src/App.tsx](frontend/src/App.tsx):
- `/`: single-project redirect or workspace project list
- `/workspace`
- `/settings/{members|api-keys|profile|security|data-sources}`
- `/settings/project/{general|members|plan-rules}`
- `/settings/instance/:instSection`
- `/p/:slug/overview`
- `/p/:slug/events`
- `/p/:slug/events/:tab`
- `/p/:slug/events/:tab/{new|:eventId|:eventId/edit}`
- `/p/:slug/monitoring/:scope/:id`
- `/p/:slug/monitors[/:monitorId]`
- `/p/:slug/metrics` and `/p/:slug/metrics/:metricId/edit`
- `/p/:slug/metrics/fact-tables[/:factTableId/edit]`
- `/p/:slug/reconciliation`, `/p/:slug/anomalies`, `/p/:slug/coverage`
- `/p/:slug/scans[/:scanId]`
- `/p/:slug/{event-types|variables|branches|alerting}[/:itemId]`
- `/p/:slug/{meta-fields|relations|history|audit}`
- `/p/:slug/settings` and `/p/:slug/settings/monitoring` (detection settings)
- Old `/p/:slug/settings/<surface>[/:itemId]` addresses (and `settings/scans`)
  redirect to the canonical paths above, so bookmarks and alert links keep working.

Main pages:
- `ProjectsPage`: project portfolio, create/demo, health rollups.
- `EventsPage` / `EventForm`: catalog, lifecycle/review triage, bulk flows,
  template-aware create/edit.
- `OverviewPage`, `MonitorsPage`, `MonitoringDetailPage`, `AnomaliesPage`:
  observation surfaces.
- `MetricsPage`, `MetricForm`, `FactTableForm`: metrics catalog and fact tables.
- `ReconciliationPage`, `CoveragePage`: governance surfaces.
- `ProjectSettingsPage`: event types, meta fields, relations, variables,
  monitoring, alerting, scans, branches, history, audit.
- `SettingsArea`: workspace, project-general, data-source, account, and instance
  configuration.

Settings tabs currently include:
- `monitoring`

## Async Pipeline Map

Scan flow:
1. A `ScanConfig` points to a `DataSource` and query.
2. API creates a `ScanJob`.
3. Celery task `tripl.worker.tasks.scan.run_scan` executes.
4. Adapter connects to the configured warehouse (ClickHouse, BigQuery, or PostgreSQL).
5. Cardinality/JSON-path analysis decides low-cardinality vs variable-like
   fields; bindings and name/group rules resolve stable identities.
6. Event generation creates or updates plan objects without overwriting authored
   field values or recreating excluded variables.
7. Job summary is written back to `ScanJob.result_summary`.

Metrics flow:
1. Beat schedules `tripl.worker.tasks.metrics.check_metrics_due` every 5 minutes (300s).
2. Due scan configs trigger collection.
3. Metrics are collected into `event_metrics`.
4. Anomalies are recalculated and persisted.
5. Alert deliveries may be created for matched rules.

Catalog metrics flow:
1. Beat schedules `check_metric_definitions_due` every 5 minutes.
2. SQL metrics query their source; fact metrics batch compatible aggregates by
   fact table; event-composition metrics reuse event series.
3. Values/breakdowns are written to `metric_values` tables.
4. Metric-scope anomalies are recalculated and can alert when a rule opts in.

Branch flow:
1. Branch changes are reviewed against a plan hash; later edits stale approvals.
2. Merge policy, ownership approvals, conflicts, and explicit resolutions gate
   the three-way merge.
3. Merge refreshes search and can best-effort create a Jira implementation
   ticket; beat polls ticket completion every 5 minutes.

Search flow:
1. Plan/metric/fact-table mutations incrementally refresh `search_documents`.
2. Keyword search is always available; optional embeddings add semantic rank.
3. Beat requeues stranded embedding batches every 15 minutes.

Alert flow:
1. Metrics/anomaly pipeline identifies matched alert-rule conditions.
2. `AlertDelivery` and `AlertDeliveryItem` records are created.
3. Celery task `tripl.worker.tasks.alerts.send_alert_delivery` sends to destination.
4. Delivery status becomes `pending`, `sent`, or `failed`.

Current alert channel support:
- Slack webhook
- Telegram bot/chat
- Generic webhook
- Email via SMTP
- Jira issue
- Linear issue
- PagerDuty Events API v2 (trigger per incident; resolve when tripl closes it —
  `worker/tasks/alerts_pagerduty.py`)
- Microsoft Teams Adaptive Card (`worker/tasks/alerts_teams.py`)

Current message formats exposed in frontend/backend types:
- `plain`
- `slack_mrkdwn`
- `telegram_html`
- `telegram_markdownv2`

## Where To Look First

If the task is about who can see or edit a project (membership, 404 for
non-members, `my_role` / `can_mutate`):
- `backend/src/tripl/services/project_access.py`
- `backend/src/tripl/services/project_member_service.py`
- `backend/src/tripl/api/v1/project_members.py`
- `backend/src/tripl/api/deps.py` (`require_project_membership`,
  `require_project_mutation_access`)
- `frontend/src/pages/settings-area/ProjectMembersSection.tsx`
- backend tests: `test_project_membership.py` (route audit plus behaviour),
  `test_project_mutation_authorization.py`; the shared helper
  `tests/_members.py` (`add_member`, `add_member_by_slug`,
  `persisted_member_user`) makes a non-owner test user a member

If the task is about event catalog CRUD:
- `backend/src/tripl/api/v1/events.py`
- `backend/src/tripl/services/event_service.py`
- `backend/src/tripl/schemas/event.py`
- `frontend/src/pages/EventsPage.tsx`
- `frontend/src/api/events.ts`
- `backend/src/tripl/tests/test_events.py`

If the task is about event types, fields, relations, meta fields, or variables:
- matching files in `backend/src/tripl/api/v1`
- matching service and schema files
- `backend/src/tripl/services/variable_value_drift_service.py`
- `backend/src/tripl/core/analyzers/_event_generator_variables.py`
- `frontend/src/pages/ProjectSettingsPage.tsx`
- `frontend/src/pages/settings/VariablesTab.tsx`
- backend tests: `test_event_types.py`, `test_fields.py`, `test_relations.py`,
  `test_meta_fields.py`, `test_variables.py`, `test_variable_value_drift.py`

If the task is about data sources or scans:
- `backend/src/tripl/api/v1/data_sources.py`
- `backend/src/tripl/api/v1/scans.py`
- `backend/src/tripl/services/datasource_service.py`
- `backend/src/tripl/services/scan_service.py`
- `backend/src/tripl/worker/tasks/scan.py`
- `backend/src/tripl/worker/tasks/scan_dry_run.py`
- `backend/src/tripl/core/adapters/clickhouse.py`
- `backend/src/tripl/tests/test_data_sources.py`
- `backend/src/tripl/tests/test_scans.py`
- `backend/src/tripl/tests/test_scan_dry_run.py`
- `frontend/src/pages/DataSourcesPage.tsx`
- `frontend/src/pages/ProjectSettingsPage.tsx`

If the task is about metrics or anomaly detection:
- `backend/src/tripl/api/v1/metrics.py`
- `backend/src/tripl/api/v1/metrics_catalog.py`
- `backend/src/tripl/api/v1/fact_tables.py`
- `backend/src/tripl/services/metrics_service.py`
- `backend/src/tripl/services/metric_definition_service.py`
- `backend/src/tripl/services/metric_series_service.py`
- `backend/src/tripl/worker/tasks/metrics/`
- `backend/src/tripl/core/analyzers/anomaly_detector.py`
- `backend/src/tripl/models/event_metric.py`
- `backend/src/tripl/models/metric_anomaly.py`
- `backend/src/tripl/tests/test_metrics_api.py`
- `backend/src/tripl/tests/test_metrics_tasks.py`
- `backend/src/tripl/tests/test_anomaly_detector.py`
- `backend/src/tripl/tests/test_project_anomaly_settings.py`
- `frontend/src/pages/MonitoringDetailPage.tsx`
- `frontend/src/pages/metrics/`
- `frontend/src/pages/fact-tables/`
- `frontend/src/lib/metrics.ts`

If the task is about alerting:
- `backend/src/tripl/api/v1/alerting.py`
- `backend/src/tripl/services/alerting_service.py`
- `backend/src/tripl/schemas/alerting.py`
- `backend/src/tripl/worker/tasks/alerts*.py`
- `backend/src/tripl/worker/tasks/alerts_*.py`
- `backend/src/tripl/alert_templates.py`
- `backend/src/tripl/alerting_validation.py`
- `backend/src/tripl/models/alert_*.py`
- `backend/src/tripl/tests/test_alerting.py`
- `frontend/src/pages/alerting/`
- `frontend/src/api/alerting.ts`

If the task is about branches, revisions, or implementation tracking:
- `backend/src/tripl/api/v1/plan_branches.py`
- `backend/src/tripl/services/plan_branch_*.py`
- `backend/src/tripl/api/v1/project_tracker_config.py`
- `backend/src/tripl/worker/tasks/implementation_tickets.py`
- `frontend/src/pages/settings/BranchesTab.tsx`
- backend tests: `test_plan_branches.py`, `test_implementation_tickets.py`

If the task is about search or AI:
- `backend/src/tripl/api/v1/search.py`, `backend/src/tripl/api/v1/ai.py`
- `backend/src/tripl/services/search_service.py` (public surface + index writes),
  `backend/src/tripl/services/_search_documents.py` (document building),
  `backend/src/tripl/services/_search_query.py` (all ranking SQL and scoring)
- `backend/src/tripl/worker/tasks/search.py`,
  `backend/src/tripl/worker/search_reindex.py` (sync-worker bridge)
- `frontend/src/components/command-palette.tsx`
- backend tests: `test_search.py`, `test_search_incremental_reindex.py`,
  `test_search_embed_task.py`
- ranking is NOT covered by the backend suite (SQLite has no stemmer) — it is
  executed only by `backend/src/tripl/tests/relevance/`, which needs a real
  PostgreSQL; see CONTRIBUTING.md, "Search relevance harness"

## Practical Coding Guidance

Project-specific expectations:
- keep FastAPI routers thin; business rules go in services;
- use Celery for heavy, retryable, or scheduled analytics work;
- prefer extending existing adapters/analyzers/tasks rather than inventing parallel paths;
- preserve async request paths; sync DB access is already used inside worker tasks and is acceptable there;
- keep frontend API wrappers typed through `frontend/src/types/index.ts`;
- when changing schemas or payloads, update both backend Pydantic models and frontend TS types;
- if you change alert message variables or formats, update both backend template logic and `ProjectAlertingTab` helper UI;
- if you change scan or metrics summaries, check any frontend assumptions around `ScanJob.result_summary`.
- style status colour with the tone tokens — `bg-<tone>-soft` + `text-<tone>` (success/warning/danger/info), or `<Chip tone>` / `<Badge variant>` — never a raw Tailwind shade like `text-amber-700`. Each `--<tone>` is pinned as the AA-safe ink for its own soft fill in *both* themes, so it needs no `dark:` twin; `frontend/src/theme-contrast.test.ts` measures it and `frontend/src/raw-palette.test.ts` fails the build if a raw shade reappears.
- **update the docs when you change files**: any change to behavior, the HTTP API, config/env, or a user-facing feature must update the documentation site under `website/docs/` in the SAME change; regenerate the OpenAPI spec with `./bin/dump-openapi.sh` when the HTTP API changed.

Operational assumptions to preserve unless intentionally changing them:
- RabbitMQ is the Celery broker.
- PostgreSQL is the system of record for catalog, metrics, anomalies, and alert deliveries.
- Warehouses (ClickHouse, BigQuery, PostgreSQL) are read from external data sources and are not the app database.
- API, worker, and beat should all be runnable together via Compose.

## Commands

Prefer the root `Makefile` — run `make` (or `make help`) for the grouped list.
It wraps the underlying uv/bun/compose commands so common flows are one keystroke
from the repo root. The ones you'll reach for most:
- `make check` — every gate (lint + typecheck + tests), CI parity
- `make sync-types` — regenerate `backend/openapi.json` + `frontend/src/types/api.gen.ts` after any HTTP API change (guarded by `test_openapi_contract`)
- `make test-be ARGS="-k diff -v"` / `make test-fe ARGS=BranchesTab` — scoped tests
- `make dev` — full stack via `compose.dev.yaml` (watch mode)

The raw commands the targets wrap (still the source of truth):

Backend:
- `uv sync`
- `uv run pytest`
- `uv run ruff check`
- `uv run ruff format --check`
- `uv run mypy`

Frontend:
- `bun install`
- `bun run lint` (oxlint with the project rules in `oxlint-plugins/`, then their tests)
- `bun run test`
- `bunx tsc -b` (TypeScript 7)

### Running tests: no database, no services

Backend tests do **not** touch Postgres. `backend/src/tripl/tests/conftest.py`
hardcodes an in-memory SQLite engine (`sqlite+aiosqlite:///:memory:`) and
overrides the app's session dependency, so `uv run pytest` needs no Postgres,
RabbitMQ, Redis, warehouse, or compose stack — and no `alembic upgrade`
beforehand. Frontend Vitest suites likewise mock all HTTP; no backend needs
to be running.

If a test run fails with connection errors mentioning
`postgresql+asyncpg://tripl:tripl@localhost:5432/tripl`, the tests are being
run the wrong way — that URL is the app-runtime default from
`backend/src/tripl/config.py`, and pytest never connects to it. Usual causes:

- bare `pytest` / `python -m pytest` from a system or pip venv instead of
  `uv run pytest` (the only supported env — uv provisions Python 3.14 and
  deps from `uv.lock`);
- running from the repo root — backend tests run from `backend/`
  (or use `make test-be ARGS="..."` from the root, which scopes for you);
- "preparing" a database first (`docker compose up`, `alembic upgrade head`,
  exporting `DATABASE_URL`) — tests neither need nor read any of that;
  `alembic upgrade head` *does* require a live Postgres and is only for the
  compose dev stack, never a test prerequisite.

pytest-asyncio runs in `auto` mode with a session-scoped event loop (pinned in
`backend/pyproject.toml`); do not add `event_loop` fixtures or `asyncio_mode`
overrides — pytest-asyncio 1.x ignores custom loop fixtures and the shared
in-memory SQLite connection depends on the single session loop.

Scoped runs:
- `cd backend && uv run pytest src/tripl/tests/test_events.py -k "diff" -q`
- `cd frontend && bunx --bun vitest run src/pages/settings/BranchesTab.test.tsx`

Compose:
- `docker compose up -d --build`
- `docker compose config`

Useful when changing DB schema:
- `uv run alembic upgrade head`

### Sandbox execution (read this first if commands fail)

If your shell runs inside a restricted sandbox — Codex's managed sandbox does by
default — the friction you hit here is almost always the **environment**, not the
code or the tests. Do **not** rewrite code, edit `conftest.py`, or "fix" the test
setup to work around these. Known cases, with the exact workaround:

- **`~/.cache/uv` is read-only.** `uv` / `make` / `./bin/*.sh` die with
  `Could not create temporary file … Read-only file system (os error 30)`
  (this breaks `make sync-types`, `dump-openapi.sh`, and any `uv run`). Redirect
  the cache to a writable dir: prefix the command with `UV_CACHE_DIR=/tmp/uv-cache`,
  e.g. `UV_CACHE_DIR=/tmp/uv-cache make sync-types`.
- **Backend `pytest` hangs forever inside the sandbox.** The asyncio loop never
  wakes after the `aiosqlite` worker-thread callback, so the async in-memory
  SQLite suite stalls indefinitely — it is **not** a deadlock in the code, and the
  same tests pass outside the sandbox. Run backend tests with escalated
  permissions (outside the seccomp/landlock sandbox). In Codex that is
  `exec_command(…, sandbox_permissions: "require_escalated")`. Adding an
  `event_loop` fixture or `asyncio_mode` override does **not** fix this and breaks
  the shared session loop (see the pytest-asyncio note above) — escalate instead.
- **`.git` is read-only in the sandbox.** `git add/commit/pull/push` and `bd`'s
  auto-export (it runs `git add` after every mutation) fail with
  `.git/index.lock: Read-only file system`. Run all `git` and `bd` write commands
  with escalated permissions.
- **Docker is unavailable.** `docker compose config` / `up` fail with
  `permission denied … /var/run/docker.sock`. You cannot validate Compose from
  inside the sandbox — say you skipped it, don't claim it passed.
- **Benign startup noise on every backend command.** Expect
  `Startup service-override apply skipped: app_settings read failed` followed by a
  SQLAlchemy `Traceback` reaching for local Postgres. This is normal: the app
  probes runtime settings in Postgres, fails, and tests continue on in-memory
  SQLite. Do not investigate it.
- **New branches need an upstream.** After the first commit, push with
  `git push -u origin <branch>` (escalated). Otherwise `git pull --rebase` fails
  with `no tracking information for the current branch`.

The cleaner alternative, if the harness allows it, is to grant the sandbox write
access to `.git`, `~/.cache/uv`, and the Docker socket up front — then none of the
above bites.

Cadence for long tasks: the full backend `make check` is slow (many minutes to
crawl to 100%). Run it (escalated) only after a big iteration; between iterations
use targeted runs — `make test-be ARGS="-k <name>"` / `make test-fe ARGS=<Name>`.

## Validation Expectations

Minimum checks before finishing:
- backend tests for touched backend domains;
- frontend tests for touched frontend domains;
- lint/type checks for the side you changed;
- `docker compose config` when Compose or env wiring changes.
- **docs updated**: behavior / API / config / feature changes are reflected in `website/docs/` (and `./bin/dump-openapi.sh` re-run if the HTTP API changed). Docs are not optional follow-up — they ship with the change.
- **API types synced**: any change to the HTTP API surface (routes, request/response models, status codes) must regenerate `backend/openapi.json` + `frontend/src/types/api.gen.ts` via `make sync-types` — `test_openapi_contract` fails on a stale snapshot, and the frontend types drift silently otherwise.

Extra checks expected for specific areas:
- scan/data-source changes: verify connection test or scan execution path;
- metrics/anomaly changes: verify at least one real collection path and anomaly output path;
- alerting changes: verify at least one delivery path and relevant template/validation behavior;
- schema contract changes: verify both backend schema and frontend type/client usage.

## PR Notes

Use PR title format:
- `[analytics] <Title>`

Always call out:
- API contract changes;
- event schema changes;
- queue/task/schedule changes;
- metrics or anomaly semantics changes;
- alerting channel/template changes;
- environment variable changes.

**After opening a PR, wait for the Copilot review and answer it — the PR is not
done when the branch is pushed.** Copilot posts an automatic review within a few
minutes of `gh pr create`; read it with
`gh api repos/tripl-io/tripl/pulls/<n>/comments`, since `gh pr view` shows only
the summary and hides the inline comments where the substance is. It has caught
real defects here more than once (stale counts in `website/docs/run/cli.md` on
#79, twice in a row). Treat every point as a claim to verify, not an instruction
to obey: check it against the code, then either fix it or reply with the evidence
that it is wrong — an unanswered comment reads as an accepted one. Also wait for
CI (`gh pr checks <n>`) before calling the work finished; poll it with
`until ! gh pr checks <n> 2>&1 | grep -q pending; do sleep 30; done`.

Copilot reviews **once, when the PR opens**. Commits pushed afterwards are not
re-reviewed, and the re-review cannot be requested from the CLI — the bot is not
a collaborator (`requested_reviewers` returns 422) and this schema has no
`CAN_BE_REVIEWER` suggested-actor filter. Either land the substance before
opening the PR, or re-request the review from the PR page and say in the thread
what the later commits changed.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for how sync works elsewhere.

The JSONL exports are gitignored on purpose: this repository is public, and the export carries the maintainer's email plus write-ups naming the private production host. Beads is local-only here — the owner's decision, after a push published the whole issue database — so do not run `bd dolt push` or `bd dolt remote add`; `bd dolt commit` is the only sync-shaped command to use. Do not re-add the exports.

## Session Completion

Before ending a session, leave nothing only on this machine that someone else needs: file beads issues for follow-up work, close what is finished, and make sure committed work is on a pushed branch.

Changes reach `main` through a pull request unless the owner asks for a direct push: a merge happens on green CI and with the owner's go-ahead. Merging `main` does not deploy the product (production is deployed by hand); it does publish the docs site to GitHub Pages. Before every push, `git diff origin/main...HEAD -- .beads/` must be empty — `bd` sometimes commits a config change to local `main` on its own. Pushing a branch is a git operation only; it never includes `bd dolt push`.
<!-- END BEADS INTEGRATION -->
