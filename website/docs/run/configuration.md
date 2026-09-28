---
title: Configuration Reference
sidebar_position: 2
---

# Configuration Reference

tripl is configured entirely through environment variables. The backend reads
them into a single [`Settings`](https://github.com/vladenisov/tripl/blob/main/backend/src/tripl/config.py)
object (Pydantic `BaseSettings`); values can come from the process environment
or from a `.env` file in the backend working directory (`model_config = {"env_file": ".env", "extra": "ignore"}`).
Unknown variables are ignored, so a single `.env` can hold backend, frontend,
and Docker Compose values side by side.

The canonical starting point is
[`.env.example`](https://github.com/vladenisov/tripl/blob/main/.env.example).
Copy it to `.env` and fill in real values.

:::note Env-var names
Every setting below is matched case-insensitively by its uppercased field name:
the `database_url` field is set with `DATABASE_URL`, `rate_limit_login_per_minute`
with `RATE_LIMIT_LOGIN_PER_MINUTE`, and so on. Defaults shown are the in-code
defaults from `Settings`; the production [`compose.yaml`](https://github.com/vladenisov/tripl/blob/main/compose.yaml)
overrides several of them, as noted.
:::

## How `DEBUG` changes everything

`DEBUG` is the master switch that decides whether tripl runs in a forgiving
development posture or a locked-down production one.

- **Default:** `false`.
- **Accepted spellings** (normalized before validation): `release`, `prod`,
  `production` are treated as `false`; `dev`, `development` are treated as
  `true`. Any other value is parsed as a normal boolean.
- When `DEBUG=false`, the FastAPI lifespan calls
  `Settings.assert_production_ready()`, which **refuses to start** the process
  unless required secrets are set (see [Production startup checks](#production-startup-checks-assert_production_ready)).
- `DEBUG` also affects CORS resolution: in debug, an empty allow-list falls back
  to `*`.

:::warning
`assert_production_ready()` runs from the FastAPI app lifespan. CLI tools and
the test suite that import `Settings` directly are **not** gated by it — only a
running API process enforces the checks.
:::

---

## Database & Broker

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://tripl:tripl@localhost:5432/tripl` | Yes (must not keep dev creds) | **Async** SQLAlchemy URL used by the FastAPI app and Alembic migrations (asyncpg driver). |
| `SYNC_DATABASE_URL` | `postgresql+psycopg://tripl:tripl@localhost:5432/tripl` | Yes (must not keep dev creds) | **Sync** SQLAlchemy URL used by the Celery worker (psycopg driver). |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672//` | Yes (must not keep dev creds) | Celery broker AMQP URL. |
| `REDIS_URL` | `""` (empty) | No | Cache backend. **Empty disables caching entirely** — every read falls through to PostgreSQL. |

:::danger Async vs sync URLs are not interchangeable
tripl maintains **two** PostgreSQL URLs pointing at the same database:
`DATABASE_URL` uses the async `asyncpg` driver for the web app, while
Alembic, while `SYNC_DATABASE_URL` uses the synchronous `psycopg` driver for
Celery. Keep host, port, database, and credentials identical between them; only
the `+asyncpg` / `+psycopg` driver suffix differs. Percent-encode reserved
characters in URL credentials (for example, `@` as `%40`); Alembic preserves
the encoded URL when loading its configuration.
:::

In the production [`compose.yaml`](https://github.com/vladenisov/tripl/blob/main/compose.yaml)
these are derived from compose-level secrets (the broker user is `tripl`, not
`guest`):

```yaml
DATABASE_URL: postgresql+asyncpg://tripl:${POSTGRES_PASSWORD}@postgres:5432/tripl
SYNC_DATABASE_URL: postgresql+psycopg://tripl:${POSTGRES_PASSWORD}@postgres:5432/tripl
RABBITMQ_URL: amqp://tripl:${RABBITMQ_PASSWORD}@rabbitmq:5672//
REDIS_URL: redis://redis:6379/0
```

---

## Identity & Secrets

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `ENCRYPTION_KEY` | `""` | **Yes** | Fernet key encrypting data-source and alert-destination secrets at rest. Must be a valid Fernet key. |
| `SECRET_KEY` | `""` | **Yes** | Application secret keying the HMAC over session tokens. Rotating it invalidates all existing sessions (users re-login once). |
| `APP_BASE_URL` | `""` | Effectively yes¹ | Public base URL of the deployment. Used to derive CORS origins when `CORS_ALLOW_ORIGINS` is empty. |
| `SESSION_COOKIE_NAME` | `tripl_session` | No | Name of the session cookie. |
| `SESSION_TTL_HOURS` | `168` (24×7) | No | Session lifetime in hours. |
| `SESSION_COOKIE_SECURE` | `false` | **Yes (must be `true`)** | Marks the session cookie `Secure` so it is only sent over HTTPS. |
| `DEBUG` | `false` | n/a | Master dev/prod switch — see [above](#how-debug-changes-everything). |

¹ `APP_BASE_URL` is not checked by name, but if `CORS_ALLOW_ORIGINS` is empty
the production check fails unless `APP_BASE_URL` supplies an origin.

Generate the secrets:

```bash
# ENCRYPTION_KEY (Fernet)
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'

# SECRET_KEY (any long random value)
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

---

## Edge & Hardening

### CORS

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `CORS_ALLOW_ORIGINS` | `""` | Effectively yes¹ | Comma-separated explicit origin allow-list. |

Effective origins are resolved by `Settings.cors_origins()` in this order:

1. If `CORS_ALLOW_ORIGINS` is set, split on commas (whitespace trimmed).
2. Else if `DEBUG=true`, fall back to `["*"]`.
3. Else if `APP_BASE_URL` is set, use it (trailing slash stripped).
4. Else deny all (`[]`).

### Security headers

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `SECURITY_HEADERS_ENABLED` | `true` | No | Toggles the security-headers middleware. |
| `HSTS_ENABLED` | `false` | No | Adds `Strict-Transport-Security`. Only safe behind HTTPS with secure cookies. |
| `HSTS_MAX_AGE_SECONDS` | `31536000` (1 year) | No | `max-age` for HSTS. |
| `CONTENT_SECURITY_POLICY` | `""` | No | Optional CSP. Left unset by default. When `SERVE_FRONTEND` is on and this is empty, a SPA-appropriate CSP is applied automatically. |

:::tip
The production `compose.yaml` sets `SECURITY_HEADERS_ENABLED=true` and defaults
`HSTS_ENABLED` to `true` (overridable via the `HSTS_ENABLED` env). Enable HSTS
only once you serve over HTTPS exclusively.
:::

### Frontend serving

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `SERVE_FRONTEND` | `false` | No | When `true`, the API also serves the built SPA from `FRONTEND_DIST_DIR`, so production runs a single container. |
| `FRONTEND_DIST_DIR` | `""` | No | Path to the built SPA assets served when `SERVE_FRONTEND` is on. |

:::note
In the published image, `SERVE_FRONTEND` / `FRONTEND_DIST_DIR` are baked in —
the single `app` container serves the JSON API and the SPA on port `8000`. In
development the Vite dev server serves the SPA with HMR and proxies `/api` to
the backend, so these stay at their defaults.
:::

### Registration

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `REGISTRATION_MODE` | `open` | **Decide it** | Who may create an account. `open` (**the default**) allows self-service signup — anyone who can reach the instance becomes a **member** of the default organization, who can read the member roster and create projects of their own. A member sees no existing project until the project's creator or an organization owner or admin adds them (**Settings → Project → Access**). Data source connection details (host, port, username) are for organization owners and admins only. `disabled` refuses `POST /auth/register` with `403` and hides the sign-up form. `open` is the default for historical reasons — it used to be the only way to onboard anyone. An owner can now invite people directly (**Settings → Organization → Invitations → Invite a member**), so `disabled` no longer blocks onboarding; set it once your team has accounts. With `DEPLOYMENT_MODE=self_hosted` the first registration on an **empty** instance is always allowed and becomes the default organization's owner and the platform admin. With `hosted` there is no such exception: `disabled` refuses every sign-up, the first one included, and each sign-up creates an organization of its own instead of joining the default one (see [Hosted sign-up and email verification](../administer/admin-guide.md#hosted-sign-up-and-email-verification)). Self-service sign-up never grants platform admin on a hosted instance, whatever `PLATFORM_ADMIN_EMAILS` lists; a listed address becomes a platform admin only when its account confirms the emailed verification link while signed in as itself. Overridable at runtime in **Settings → Instance → Security & access**, where it applies immediately. See [Security & Hardening](./security.md#self-service-registration). |

### Organizations

Every project, data source and API key belongs to an organization, and a user
can belong to several. An instance starts with one **default organization**;
access follows the organization role (`owner`, `admin` or
`member`) and the project role. These settings are environment-only — they
cannot be changed in **Settings → Instance**.

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `DEPLOYMENT_MODE` | `self_hosted` | No | `self_hosted` is one team's instance. `hosted` is a multi-tenant service. It is read when the database is migrated to decide who becomes a **platform admin** (the operator of the whole instance). When the value is `self_hosted`, every owner of the default organization becomes a platform admin, and so does the first account of an empty instance. When the value is `hosted`, only existing accounts whose address is in `PLATFORM_ADMIN_EMAILS` do. It also decides which organization a request acts in when its URL does not name one (no `/api/v1/orgs/{org}/` prefix): `self_hosted` always acts in the default organization; `hosted` acts in the user's only organization and answers 400 `Organization required` when the user belongs to none or to several (such a user names the organization in the URL). With `hosted`, sign-up creates a new organization, signing up needs the operator's SMTP, and an account must verify its email address before it can use anything beyond the `/api/v1/auth/*` routes; any signed-in user may create an organization. A new account made from an invitation on a hosted instance must verify its address too. With `self_hosted` none of that applies: every account is marked verified when it is created and nothing is ever gated. See [Hosted sign-up and email verification](../administer/admin-guide.md#hosted-sign-up-and-email-verification). Any other value refuses to start. The platform-admin grant runs during the upgrades that add organizations (the organization schema, and again with organization roles). It only adds the flag and never revokes it. Changing the value between upgrades changes nothing until the next such upgrade. After that, platform admins are granted and revoked in the [platform console](../administer/admin-guide.md#platform-console) or with [`tripl-admin`](#tripl-admin). |
| `PLATFORM_ADMIN_EMAILS` | empty | Only when `hosted` | Comma-separated account emails that become platform admins when `DEPLOYMENT_MODE=hosted`. Case and surrounding spaces are ignored. Only accounts that already exist when an organization upgrade runs are flagged, so create them first and set the list before upgrading. Signing up, accepting an invitation or completing a password reset with a listed address **never** grants it. Through the list, the only way is the emailed verification link: when an account with a listed address confirms that link while signed in as itself, it becomes a platform admin at that moment, so the grant follows proof of both the mailbox and the account. Removing an email revokes nothing; revoke in the [platform console](../administer/admin-guide.md#platform-users) or with [`tripl-admin`](#tripl-admin). An operator who cannot receive mail on a listed address can grant the flag with `tripl-admin` instead. |
| `ORG_SETTINGS_OPERATOR_FALLBACK` | `all` | No | Covers an organization that has not set its own AI, SMTP or search-embedding settings. `all` means it uses the operator's; `none` means those features are off for it (the organization page shows **Disabled by operator policy**) until it sets its own relay, AI endpoint or embedding endpoint and key. With `none`, an organization without its own embedding endpoint has semantic search off; lexical search still works. Non-secret values (row limits, AI timeout and token limits, prompts, the AI switch) fall back to the operator either way. Account mail (email verification, password reset, invitations) always uses the operator's SMTP. On a `self_hosted` instance the default organization's settings **are** the operator's, so this has no effect there; it matters for every other organization. |

### Managing platform admins: `tripl-admin` {#tripl-admin}

`tripl-admin` is a console command installed with the backend (it is on the
`PATH` of the tripl image). It works on the database directly, over the same
`SYNC_DATABASE_URL` the Celery workers use, so it needs no running app, no
session and no mail. It is how the operator of a hosted instance makes the
first platform admin, and the way back in when every platform admin has lost
access; after that, the [platform console](../administer/admin-guide.md#platform-users)
does the same from the browser.

```bash
# In a compose deployment, from the directory holding compose.yaml and .env:
docker compose exec app tripl-admin grant-platform-admin ops@example.com
docker compose exec app tripl-admin list-platform-admins
docker compose exec app tripl-admin revoke-platform-admin former-ops@example.com
```

| Command | What it does |
| --- | --- |
| `grant-platform-admin EMAIL` | Makes the account with that address a platform admin, and marks its address verified if it was not (recorded as `marked_verified: true` in the audit entry). Granting an existing admin changes nothing. |
| `revoke-platform-admin EMAIL` | Takes the flag away. Refuses to revoke the **last** platform admin, so the instance always keeps one. |
| `list-platform-admins` | Prints the address of every platform admin. |

The address is matched without regard to case and must belong to an existing
account: create it first (sign up, or accept an invitation). An unknown address
or a refused revocation prints the reason and exits non-zero, so a provisioning
script can stop on it. The command does not look at `DEPLOYMENT_MODE` or
`PLATFORM_ADMIN_EMAILS` and does not require the address to be verified:
whoever can run it on the server already controls the instance, so a grant
marks an unverified address verified instead.

### Operator and organization settings

Every setting editable in the UI is either the **operator's** (tripl's own
infrastructure, one value for the whole instance) or an **organization's** (each
organization may set its own value). An organization resolves each of its fields
as **its own value → the operator's value → the environment variable → the
built-in default**, subject to three rules:

- **A secret travels with its endpoint.** The AI endpoint, key and model form
  one group; the SMTP host, port, security, username, password and From:
  address form another; the search-embedding base URL, provider, model and key
  form a third; the photo storage backend, GCS bucket, service-account JSON,
  public-URL switch and signed-URL lifetime a fourth. An organization that sets any field of a group owns the
  whole group: fields it left empty take the built-in default (a secret is
  empty), never the operator's. Pointing `ai_base_url`, `smtp_host` or
  `search_embedding_base_url` at your own server therefore never sends the
  operator's key or password there, and an organization's own bucket is never
  written with the server's GCS credentials.
- **Operator ceilings.** An organization's `scan_row_limit_default`,
  `metrics_row_limit_default`, `ai_timeout_seconds`, `ai_max_output_tokens`
  and `photo_max_size_mb` may lower the operator's value, never raise it: a
  higher value is refused on save (`422`) and clamped at use if the operator
  later lowers theirs. Likewise an organization's `photo_allowed_mime` may only
  narrow the operator's allow-list: a content type the operator does not allow
  (SVG, HTML) is refused on save and dropped at use.
- **Public hosts only, for organizations.** An organization's `ai_base_url`,
  `search_embedding_base_url` and `smtp_host` must not be (or resolve to) a
  private, loopback or link-local address, in either deployment mode. The check
  runs on save (`422`) and again right before each request, so a name
  re-pointed at an internal address later is refused too (the feature is off
  for that call). An AI or embedding endpoint that answers with a redirect is
  refused rather than followed (on a `hosted` instance the operator's embedding
  endpoint too). The operator's own
  values — which on a `self_hosted` instance are also the default
  organization's — may point at private hosts such as a local model server or
  relay.

If an organization's settings cannot be read, background jobs (alerts, digests,
notification email, AI explanations, search embedding) run with AI, email and
embeddings off for that organization; a request fails rather than falling back
to the operator's keys.

**Search embeddings per organization.** Each organization has its own vector
space. Vectors are stamped with the organization's *provenance* — its endpoint,
provider, model and the dimensions — and a query only compares with rows
stamped with the provenance of the organization it runs in. An organization
that inherits the operator's endpoint has exactly the operator's provenance, so
introducing organizations re-embeds nothing. `SEARCH_EMBEDDING_DIMENSIONS`
stays the operator's (the column is `vector(1536)` for everyone): when an
organization saves its own endpoint or model with embeddings on, tripl embeds
one short test text with it and refuses the save (`422`) unless the answer is a
1536-value vector. A save that changes an organization's embedding identity
(on/off, endpoint, provider or model) queues a reindex of **that organization's
projects only**; unchanged rows keep their vectors. A change to the operator's
values reaches the organizations that inherit them through the regular stale
sweep. The operator's own base URL stays env-only (`SEARCH_EMBEDDING_BASE_URL`):
on a `self_hosted` instance the default organization cannot change it in
settings (`422`).

**Photo storage per organization.** An organization's owners and admins can
give it storage of its own under **Settings → Organization → Photos**: a GCS
bucket with the organization's own service-account JSON key (pasted in the
page, encrypted at rest, never returned: the response only says whether one is
configured). On a `self_hosted` instance an organization other than the default
one may pick the `local` backend instead (the operator's `PHOTO_LOCAL_DIR`); on
a `hosted` instance it may not (`422`). The server paths `PHOTO_LOCAL_DIR` and
`GCS_PHOTO_CREDENTIALS_PATH` are the operator's alone. An organization without
storage of its own uses the operator's, whatever
`ORG_SETTINGS_OPERATOR_FALLBACK` says. The save is refused (`422`) for a GCS
bucket without a key of its own, a key that is not a loadable service-account
JSON, a key whose `token_uri` is not `https://oauth2.googleapis.com/token` or
whose `universe_domain` is not `googleapis.com` (the server would otherwise
send its token requests wherever the key says), or an invalid bucket name. Changes apply to the next upload: every photo
row records the organization and the **storage version** it was written with
(a `photo_storage_configs` row per distinct bucket/key/flags), and each photo is
read and deleted through that version, so moving to another bucket or rotating
the key keeps older photos working as long as the old bucket and key still
grant access. New uploads are keyed `orgs/{organization id}/events/...` on
whichever store they land in. Everyone in the organization can read its upload
limits at `GET /api/v1/orgs/{org}/settings/photo-limits`. Deleting an
organization deletes its photos through its own storage. The
Content-Security-Policy admits `https://storage.googleapis.com` images for the
pages of an instance where the operator or any organization stores photos in
GCS, and per organization for API responses; a policy set with
`CONTENT_SECURITY_POLICY` is used as given.

**Tracker defaults per organization.** An organization's owners and admins can
set Jira and Linear defaults under **Settings → Organization → Trackers**: the
Jira site, account e-mail, API token and default project key, and a Linear API
key and default team. A project's own tracker config overrides each field; a
project still has to switch the automation on itself. The Jira site, account
and token are one unit: a project that sets any of the three uses none of the
organization's. Tokens are encrypted at rest and never returned; the Jira site
must be `https` and public (checked on save and before every call).

| Setting | Scope | Where it is edited |
| --- | --- | --- |
| Public URL (`APP_BASE_URL`) | Operator | **Settings → Platform** |
| Security & access (CORS, cookies, headers, rate limits, `REGISTRATION_MODE`) | Operator | **Settings → Platform** |
| Observability (request id, logging, metrics, tracing) | Operator | **Settings → Platform** |
| `PHOTO_LOCAL_DIR`, `GCS_PHOTO_CREDENTIALS_PATH` (server paths) | Operator | **Settings → Platform** |
| Photo storage: backend, GCS bucket, public URLs, URL lifetime, service-account JSON (organization only) | Organization (inherits the operator's store when unset) | **Settings → Organization → Photos** |
| `PHOTO_MAX_SIZE_MB`, `PHOTO_ALLOWED_MIME` | Organization (capped by / a subset of the operator's) | **Settings → Organization → Photos** |
| Search embeddings: switch, provider, model, key, base URL | Organization (the operator's base URL is env-only) | **Settings → Organization → Search** |
| `SEARCH_EMBEDDING_DIMENSIONS` | Operator, env-only (1536) | read-only |
| Jira / Linear tracker defaults (site, account, token, project; key, team) | Organization (no operator layer; projects override) | **Settings → Organization → Trackers** |
| Database, broker, Redis, `ENCRYPTION_KEY`, `SECRET_KEY` (the "system" block) | Operator, env-only | read-only in **Settings → Platform** |
| Email / SMTP (6 fields) | Organization | **Settings → Organization → Email** |
| AI chat: switch, base URL, model, key, timeout, output tokens, the three prompts | Organization | **Settings → Organization → AI** |
| `SCAN_ROW_LIMIT_DEFAULT`, `METRICS_ROW_LIMIT_DEFAULT` | Organization (capped by the operator) | **Settings → Organization → Limits** |

The operator's values of the organization fields are the defaults every
organization inherits (with `ORG_SETTINGS_OPERATOR_FALLBACK=all`), and the
operator's SMTP is the relay account mail always uses; the platform admin sets
them under **Settings → Platform**. On a `self_hosted` instance the default
organization **is** the operator scope: what its owners and admins set under
**Settings → Organization** is the instance's value. Its limits, timeouts and
prompts are theirs to change; its SMTP relay and AI endpoint (the whole group,
key and password included) take a platform admin (`403 Platform admin
required`), because password-reset and invitation mail go through that relay
and, with `ORG_SETTINGS_OPERATOR_FALLBACK=all`, every other organization
inherits both. The same applies to the embedding key, provider, model and
switch, and to the storage fields: the operator's bucket holds every inheriting
organization's photos, and its size cap and content types are every
organization's ceiling. The operator's storage values take effect when the
server restarts; an organization's own apply to its next upload. The API is `GET/PATCH/PUT /api/v1/orgs/{org}/settings` (with
`POST .../ai/test` and `.../email/test`, and `GET/PATCH .../trackers` for the
tracker defaults) for an organization's owners and admins, plus
`GET .../photo-limits` for every member,
and `GET/PATCH /api/v1/platform/settings` (with the same two probes) for a
platform admin. The older `/api/v1/settings` still answers with the combined
view: operator fields as the operator has them and organization fields as the
caller's organization runs with them. For anyone but a platform admin its
`security`, `storage`, `observability` and `system` blocks are `null` and the
embedding base URL is blank unless it is the organization's own.

### Rate limiting

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `RATE_LIMIT_ENABLED` | `true` | No | Master toggle for auth-endpoint rate limiting. |
| `RATE_LIMIT_LOGIN_PER_MINUTE` | `5` | No | Login attempts per `(ip, route)` per minute. `0` disables this limit. |
| `RATE_LIMIT_REGISTER_PER_HOUR` | `3` | No | Registrations per `(ip, route)` per hour. `0` disables this limit. |
| `RATE_LIMIT_TRUST_FORWARDED_FOR` | `false` | No | Derive client IP from `X-Real-IP` / leftmost `X-Forwarded-For` instead of the socket peer. |

:::danger Only trust forwarded headers behind a trusted proxy
With `REDIS_URL` set the buckets live in Redis and every worker shares one
quota per client; without Redis each worker keeps its own. Leave
`RATE_LIMIT_TRUST_FORWARDED_FOR=false` (the default) whenever the API is the
edge — including the consolidated single container. Enable it only when a
trusted proxy/LB overwrites `X-Real-IP` on every request; a raw, attacker-
controlled `X-Forwarded-For` on a directly exposed API lets a caller rotate it
per request and bypass the limit.
:::

---

## Observability

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `REQUEST_ID_HEADER` | `X-Request-ID` | No | Header used to read/emit a per-request correlation ID. |
| `LOG_LEVEL` | `INFO` | No | Log level (uppercased and trimmed). |
| `LOG_JSON` | `false` | No | Emit one-line JSON logs instead of plain text. Compose/k8s should enable this. |
| `PROMETHEUS_METRICS_ENABLED` | `false` | No | Exposes the `/metrics` endpoint and Celery task instrumentation. |
| `PROMETHEUS_MULTIPROC_DIR` | unset outside Compose | No | Shared writable directory for Prometheus metrics from all API and Celery worker processes. Compose sets `/app/var/prometheus` and mounts it in both services. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | No | Setting a non-empty value opts the API and worker into FastAPI/SQLAlchemy/Celery auto-instrumentation via an OTLP exporter. The production image includes the optional tracing dependencies; a blank endpoint disables export. |
| `OTEL_SERVICE_NAME` | `tripl` | No | Service name reported by the OTLP exporter. |

:::tip
`compose.yaml` defaults `LOG_JSON` to `true` (overridable); the API, Celery
worker, and beat use the configured log level and format. Expose `/metrics`
only on an internal-only ingress path or scrape via a sidecar. Compose shares
the Prometheus multiprocess directory between the API and worker and clears old
metric files before those processes start. When running without Compose, create
a writable shared directory and clear stale files before each deployment. Each
failed Celery task contributes one failure count to `tripl_celery_tasks_total`.
:::

---

## Demo workspace

Two independent switches control the generated demo project. Both default to
**on**, and neither affects real projects in any state.

| Variable | Default | Required in prod? | Purpose |
| --- | --- | --- | --- |
| `DEMO_ENABLED` | `true` | No | Master kill switch for demo **provisioning**. When `false`, `POST /projects/demo` **and** demo reset are refused with `403 Demo provisioning is disabled`. |
| `DEMO_RUNTIME_ENABLED` | `true` | No | Gates the `advance_demos` beat task that keeps an existing demo fresh (new buckets, jobs, and signals). When `false` that task is a no-op and existing demos keep the data they already have; the scheduled scan collection of a demo in use still runs on its 6-hour demo cadence and appends new buckets itself. |

:::note A demo's two refresh paths run at different rates
`advance_demos` runs **hourly**: it appends the newest bucket, re-runs the real
detector for volume anomalies, and records a scan job, so a demo always looks
live. The full scheduled collection — which additionally produces breakdown
anomalies and distribution drift — runs at most **every 6 hours** per demo
instead of hourly, because it costs 67–141 s against the in-memory dataset and
every demo on a deployment used to pay that every hour. Both paths additionally
stop for a demo nobody has opened for **6 hours** and resume on the next visit;
they share one idleness rule rather than testing it separately, because a
collection dispatched while the tick is paused opens a window reaching back past
the synthetic warehouse's full-volume hours and overwrites the demo's history
with near-zero counts. Real projects are unaffected and keep their configured
`interval`.
:::

:::note Reset is a provisioning path — delete is not
A reset re-seeds a demo from scratch, so `DEMO_ENABLED=false` blocks **Create**
and **Reset** alike. **Deleting** a demo stays available in every state of both
flags, so a workspace can never be stuck with a demo it cannot remove. Real
project create / scan / delete, and the real scan and metric schedulers, are
untouched by either flag.
:::

See [The demo workspace](../use/demo-workspace.md) for what a demo contains and
which parts of it are synthetic.

---

## Optional features

These groups are off (or unconfigured) by default. Several enable sending plan
content or photos to external providers, so they are explicitly opt-in.

### Hybrid knowledge search (embeddings)

Lexical/fuzzy search runs locally against PostgreSQL with no extra config.
Embeddings are opt-in because indexed text may include internal tracking-plan
content sent to the configured provider.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEARCH_EMBEDDINGS_ENABLED` | `false` | Enables embedding-backed search. |
| `SEARCH_EMBEDDING_PROVIDER` | `openai` | Embedding provider. |
| `SEARCH_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. |
| `SEARCH_EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions. |
| `SEARCH_EMBEDDING_API_KEY` | `""` | Provider API key; falls back to `OPENAI_API_KEY` if empty. |
| `SEARCH_EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | Base URL of an OpenAI-compatible embeddings endpoint; `/embeddings` is appended. Point it at a self-hosted provider to keep plan text inside your own infrastructure. Env-only for the operator (an organization may set its own under **Settings → Organization → Search**), and changing it after indexing needs a re-index — see [AI and search](./ai-and-search.md). The resolved value is visible read-only under **Settings → Instance → AI**, with a source badge, so a value that never reached the container can be noticed from a browser instead of by diffing the compose file. |
| `OPENAI_API_KEY` | `""` | Shared OpenAI key used as fallback for search embeddings and AI features. |

### AI features (LLM descriptions, Q&A)

Disabled by default because plan content (event names, descriptions, field
names) is sent to the configured provider when enabled.

These are the operator's values; each organization may set its own under
**Settings → Organization → AI** (see
[Operator and organization settings](#operator-and-organization-settings)).

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_ENABLED` | `false` | Master toggle for LLM-powered features. |
| `AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL. |
| `AI_MODEL` | `gpt-4o-mini` | Chat/completion model. |
| `AI_API_KEY` | `""` | Provider API key; falls back to `OPENAI_API_KEY` if empty. |
| `AI_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `AI_MAX_OUTPUT_TOKENS` | `700` | Output token cap. |

### Email alerts (SMTP)

Leaving `SMTP_HOST` blank disables email destinations: creating them still
works, but sends fail with a friendly error pointing at this config. The worker
reads these at send time, so changes take effect without re-creating
destinations.

These are the operator's relay: account mail (sign-up, password reset,
invitations) always uses it, and organizations inherit it unless they set their
own under **Settings → Organization → Email**.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMTP_HOST` | `""` | SMTP server host. Blank disables email delivery. |
| `SMTP_PORT` | `587` | SMTP port. Has to agree with `SMTP_SECURITY`. |
| `SMTP_USERNAME` | `""` | SMTP auth username. |
| `SMTP_PASSWORD` | `""` | SMTP auth password. |
| `SMTP_SECURITY` | derived | `starttls`, `implicit_tls` or `none`. See below. |
| `SMTP_USE_TLS` | `true` | **Deprecated.** Only supplies `SMTP_SECURITY`'s default when that is unset. |
| `SMTP_FROM_ADDRESS` | `""` | Default `From:` address; may carry a display name (`Tripl Alerts <no-reply@example.com>`). Required for password-reset mail. Invalid values fail startup or the Settings save, before an alert is sent. |

`SMTP_SECURITY` names the transport, and the transport has to match the port:

| Value | What happens on the wire | Usual port |
| --- | --- | --- |
| `starttls` | Connects in the clear, reads the server greeting, then upgrades in place. | 587, 2525 |
| `implicit_tls` | Wraps the socket in TLS before sending anything, so the greeting itself is encrypted (SMTPS). | 465 |
| `none` | Plaintext for the whole session. Only reasonable for a relay on localhost or a network path you already trust. | 25 |

Getting this pair wrong does not fail fast. Pointing `starttls` at an
implicit-TLS port leaves the client waiting for a plaintext greeting that will
never arrive, so the send stalls for ten seconds and then reports a dropped
connection — which reads like a network problem rather than a configuration one.

When `SMTP_SECURITY` is unset it is derived from the deprecated `SMTP_USE_TLS`
(`true` → `starttls`, `false` → `none`), so an existing deployment keeps the
behaviour it already had. Set `SMTP_SECURITY` instead; it wins.

Settings → Email has a **Send test email** card that sends one message with
the saved settings and shows what the relay answered. It stays disabled until an
SMTP host and a default From address are saved, and it uses the saved settings, so save your changes first.
Use it after changing any of these — a failed password-reset send is
deliberately invisible to the person who asked for the link, so this is the
only place the failure surfaces.

### Event photo storage

| Variable | Default | Purpose |
| --- | --- | --- |
| `PHOTO_STORAGE_BACKEND` | `local` | `local` (filesystem, served via authenticated API endpoint) or `gcs` (Google Cloud Storage). |
| `PHOTO_LOCAL_DIR` | `./var/photos` | Directory for the `local` backend. In the shipped image this resolves to `/app/var/photos`, which is writable by the image's `app` user and mounted as the `photos` volume by `compose.yaml`. Point it elsewhere only at another mounted, writable volume, or uploads are lost when the container is recreated. |
| `PHOTO_MAX_SIZE_MB` | `10` | Max upload size in MB, and the ceiling of every organization's own cap. A request to the photo routes whose body is larger than this plus 1 MiB of multipart framing is refused with `413` without being read past that limit (before any organization is known). |
| `MAX_REQUEST_BODY_MB` | `2` | App-wide JSON/body limit in MiB. The photo upload route uses `PHOTO_MAX_SIZE_MB` plus multipart framing instead. Oversized requests return `413` before parsing or authentication. |
| `PHOTO_ALLOWED_MIME` | `image/jpeg,image/png,image/gif,image/webp` | Allowed MIME types (comma-separated). An organization's own list may only narrow it. |
| `GCS_PHOTO_BUCKET` | `""` | GCS bucket for the `gcs` backend. |
| `GCS_PHOTO_CREDENTIALS_PATH` | `""` | Service-account JSON path. Empty falls back to Application Default Credentials. Credentials that cannot sign URLs (Application Default Credentials on Compute Engine or workload identity, `gcloud` user credentials) make photos fall back to the authenticated `/file` endpoint instead of signed URLs. |
| `GCS_PHOTO_PUBLIC` | `false` | Return public URLs instead of time-limited signed URLs. |
| `GCS_PHOTO_SIGNED_URL_TTL_SECONDS` | `3600` | Signed-URL lifetime when not public. |
| `PHOTO_ORPHAN_SWEEP_GRACE_HOURS` | `24` | Minimum age, in hours, of a photo file the daily orphan sweep may delete. See below. |

**Orphan photo sweep.** Deleting an event, an event type, a project or a plan
branch removes the photo rows attached to it but not their files. Every day at
05:30 UTC a Celery task deletes photo files that no photo row references any
more and that are older than `PHOTO_ORPHAN_SWEEP_GRACE_HOURS`. Newer files are
never touched, because an upload writes its file before it saves the row. When
no photo row at all references a backend, the sweep skips that backend and logs
a warning instead of deleting: that is what an empty or half-restored database
looks like, not a directory of orphans. Only the prefixes tripl writes are
listed — `events/` (uploads before per-organization storage) and
`orgs/{organization id}/events/` for each organization that exists — so other
files in the directory or bucket are left alone. The sweep covers the `local`
backend and, when `GCS_PHOTO_BUCKET` is set, the `gcs` backend, including rows
written before a backend switch; and each organization's own bucket, with the
organization's own key and under its own prefix only. A file any photo row
names is kept, in any store. It
runs on `celery-worker`, which therefore mounts the same `photos` volume as
`app`. A worker without that mount sees an empty directory and deletes nothing.

**Switching `PHOTO_STORAGE_BACKEND` does not move anything.** Every photo row
records the backend its file was written to, and that is the backend it is read
and deleted through from then on — so photos taken before a switch keep working,
as long as the old backend stays configured. Leave `PHOTO_LOCAL_DIR` pointing at
the same volume when moving to `gcs`, and leave `GCS_PHOTO_BUCKET` set when
moving back to `local`; new uploads follow the new setting either way. Take the
old backend away and only its photos are affected: they answer `409` naming it,
the rest of the page loads, and nothing is deleted. There is no migration
command — copy the objects across yourself before retiring a backend.

### Warehouse query row caps

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCAN_ROW_LIMIT_DEFAULT` | `50000` | Default row cap for scan/replay when no scan-config override is set. |
| `METRICS_ROW_LIMIT_DEFAULT` | `100000` | Default row cap for metrics queries when no override is set. |

An organization may lower either cap for its own scans under **Settings →
Organization → Limits**; these values are its ceiling.

### Operational history retention

The daily maintenance task prunes old completed scan jobs and distribution
drift records in every project. Scan-job age starts at `completed_at`, so a
long-running job retains its full configured history after it finishes. Active
scan jobs remain available. Distribution drift in the `stable` or `minor` band
has a shorter horizon than significant drift; set the shorter horizon no higher
than the general one.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCAN_JOB_RETENTION_DAYS` | `90` | Age limit for completed scan jobs, measured from `completed_at`. |
| `DISTRIBUTION_DRIFT_RETENTION_DAYS` | `90` | Age limit for significant distribution drift. |
| `DISTRIBUTION_DRIFT_MINOR_RETENTION_DAYS` | `30` | Age limit for stable and minor distribution drift. |

---

## Production startup checks (`assert_production_ready`)

When `DEBUG=false`, the FastAPI lifespan refuses to start and raises a
`RuntimeError` listing every problem if any of the following hold:

1. **`ENCRYPTION_KEY` is empty** — data-source and alert-destination secrets
   would be stored as plaintext.
2. **`ENCRYPTION_KEY` is not a valid Fernet key** — it is validated by
   constructing `Fernet(key)`.
3. **`SESSION_COOKIE_SECURE` is false** — session cookies would be sent over
   plain HTTP.
4. **`SECRET_KEY` is empty** — session-token hashes would be unkeyed and
   guessable.
5. **Resolved CORS origins are empty** — no browser could call the API. Set
   `CORS_ALLOW_ORIGINS` or `APP_BASE_URL`.
6. **Resolved CORS origins are exactly `["*"]`** — browsers reject credentialed
   (cookie) requests against a wildcard origin, breaking session auth. Set an
   explicit origin.
7. **`DATABASE_URL`, `SYNC_DATABASE_URL`, or `RABBITMQ_URL` still contain
   dev-default credentials** — any of the markers `tripl:tripl` or `guest:guest`
   surviving into a non-debug deploy fails the check.

:::note
These checks are pure secret/edge hygiene. Optional-feature variables
(AI, search, SMTP, photo storage, OTEL, Prometheus) are **not** validated here —
they fail gracefully or stay disabled when unconfigured.
:::

---

## Compose / deployment variables

These are consumed by Docker Compose and the image, not by the backend
`Settings` object. They appear in
[`.env.example`](https://github.com/vladenisov/tripl/blob/main/.env.example) so
one `.env` covers the whole stack.

| Variable | Default | Used by | Purpose |
| --- | --- | --- | --- |
| `POSTGRES_USER` | `tripl` | PostgreSQL container | DB superuser (compose uses `tripl`). |
| `POSTGRES_DB` | `tripl` | PostgreSQL container | Database name. |
| `POSTGRES_PASSWORD` | — (required) | Compose | Builds the DB URLs. The prod stack **requires** a non-default value. |
| `RABBITMQ_PASSWORD` | — (required) | Compose | Builds `RABBITMQ_URL`; broker user is `tripl`. |
| `TRIPL_IMAGE` | `ghcr.io/vladenisov/tripl` | Compose | Published image to run. |
| `TRIPL_VERSION` | `latest` | Compose | Image tag — pin to a released tag in production. |
| `VITE_API_URL` | `http://127.0.0.1:8000` | Frontend build | Base URL the SPA calls; baked in at build time. |

:::warning Compose enforces required secrets too
In `compose.yaml`, `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD`, `ENCRYPTION_KEY`,
`SECRET_KEY`, and `APP_BASE_URL` use the `${VAR:?...}` form, so `docker compose`
fails fast with a clear message if any is unset — before the app even runs its
own `assert_production_ready()` checks.
:::

---

## See also

- [Deployment](./deployment.md) — bringing the stack up with the production
  compose file.
- [Release process](./release.md) — building and publishing the image with
  `bin/release.sh`.
- [Administration](../administer/admin-guide.md) — day-2 operations.
- [Troubleshooting](../use/troubleshooting.md) — diagnosing common failures.
- Source of truth:
  [`config.py`](https://github.com/vladenisov/tripl/blob/main/backend/src/tripl/config.py),
  [`.env.example`](https://github.com/vladenisov/tripl/blob/main/.env.example),
  [`compose.yaml`](https://github.com/vladenisov/tripl/blob/main/compose.yaml).
