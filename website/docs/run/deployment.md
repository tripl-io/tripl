---
title: Self-hosting & Deployment
sidebar_position: 1
---

# Self-hosting & Deployment

tripl ships as **one image** that serves the JSON API and the built React SPA from a single process. The same image is reused for the API, a one-shot database migration, and the Celery worker and beat scheduler — only the launch command differs. The default `compose.yaml` runs the **published release image**; there is no source build on the deploy host.

This page covers running that stack in production: prerequisites, the secrets it needs, the services in `compose.yaml`, connecting a warehouse, and upgrades.

:::tip The documented default is a command, not a checklist
**[`tripl install`](./cli.md#tripl-install)** provisions the stack and **[`tripl upgrade --to X.Y.Z`](./cli.md#tripl-upgrade)** moves it to a new tag. Read this page for *what* the stack is and *why* each piece is arranged the way it is; let the command do the copying, the secret generation and the file modes, and read the [Operator CLI](./cli.md) for exactly what it does. The by-hand equivalent of every automated step is kept, under the two **"Without the CLI"** headings, for a host that cannot run it.
:::

:::note
For tuning individual settings (logging, rate limits, metrics, AI/search features), see [configuration](./configuration). When something does not come up, see [troubleshooting](../use/troubleshooting).
:::

## Prerequisites

- **Docker Engine** with the Compose v2 plugin (`docker compose`, not the legacy `docker-compose`).
- **A TLS terminator in front of the app.** The production stack forces secure session cookies on and refuses dev-default credentials, so it expects to sit behind HTTPS. Terminate TLS in front of port `8000` (reverse proxy, load balancer, or platform ingress) and use an `https://` `APP_BASE_URL`. To preview the app over plain HTTP locally, use the dev stack instead (see [Local preview](#local-preview)).
- **Files on the deploy host:** `compose.yaml`, your `.env`, and the `infra/rabbitmq/` directory (the RabbitMQ service mounts `infra/rabbitmq/rabbitmq.conf`). `tripl install` writes all three; place them by hand only on the "Without the CLI" path. The `rabbitmq.conf` is not optional — Docker's answer to a missing bind-mount source is to create a *directory* there, after which RabbitMQ fails to start with an error naming neither tripl nor the mount.
- **[`uv`](https://docs.astral.sh/uv/getting-started/installation/) on the deploy host — only if you take the CLI path.** `uvx tripl install` fetches the CLI from PyPI at run time and brings its own Python, so `uv` is the whole prerequisite; `pip install tripl` needs Python 3.12+ instead. The **"Without the CLI"** paths below need neither, only Docker.
- **Outbound access to GHCR** (`ghcr.io`) to pull the image. If the package is private you must `docker login ghcr.io` first; see [Registry access](#registry-access).
- **A data warehouse to monitor** — ClickHouse, BigQuery, or PostgreSQL. This is connected from the UI after the stack is up, not via environment variables (see [Connecting a warehouse](#connecting-a-warehouse)).

### Rough resource sizing

`compose.yaml` defines **eight services**: `postgres`, `rabbitmq`, `redis`, the one-shot `migrate`, `app`, `celery-worker`, `celery-beat`, and the profile-gated `mcp`. A default `docker compose up -d` starts seven of them — `mcp` sits behind `--profile mcp` — and `migrate` runs once and exits, so the steady state is **six long-running containers**. The `app` container runs **4 uvicorn workers by default** (`UVICORN_WORKERS=4`, baked into the image).

A modest single-host deployment is comfortable at roughly **2 vCPU / 4 GB RAM** for trials and small teams. Give it more headroom (4+ vCPU, 8 GB+) if you connect large warehouses or run frequent scans, since warehouse queries and scans execute on `celery-worker`. Redis is capped at 256 MB (`--maxmemory 256mb`, `allkeys-lru`) and runs without persistence (`--save ""`), so it is a pure cache — losing it costs nothing but a cache warm-up. PostgreSQL holds the durable application state in the `pgdata18` volume; uploaded event photos, while they use the default local photo backend, are files in the `photos` volume. Back up both ([runbook](./runbook.md#photo-volume-backup)).

## Registry access

The image lives at `ghcr.io/tripl-io/tripl`. The first publish to a new GHCR package creates it as **private**, so anonymous pulls fail until it is made public (in the repo's *Packages → tripl → Package settings*). Until then, authenticate on the deploy host with a GitHub personal access token that has `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin
```

The image and tag are configurable via `.env`:

| Variable | Default | Notes |
|---|---|---|
| `TRIPL_IMAGE` | `ghcr.io/tripl-io/tripl` | Registry/repo of the image. |
| `TRIPL_VERSION` | `latest` | **Pin** to a released tag (e.g. `1.4.0`) in production; `latest` is fine for trials. |

Released tags are `X.Y.Z`, `X.Y`, `latest` (stable only), and `sha-<short>`. Multi-arch images are published for **`linux/amd64` and `linux/arm64`**, so the same tag runs on both architectures.

## Install with the CLI

One command writes the stack and starts it. `uvx` fetches the CLI from PyPI at run time and brings its own Python, so [`uv`](https://docs.astral.sh/uv/getting-started/installation/) is the only thing you install on the host — or `pip install tripl` and drop the `uvx` prefix:

```bash
uvx tripl install --app-url https://tripl.example.com --version 1.4.0 --dir /srv/tripl
```

It writes three files into `--dir` (default `./tripl`) — `compose.yaml` and `infra/rabbitmq/rabbitmq.conf` verbatim, and a generated `0600` `.env` — then runs `docker compose pull` and `docker compose up -d` in that directory and polls `<--app-url>/health` until it answers. `--dry-run` prints exactly what a real run would do and writes nothing; it is worth typing first.

Every flag, the plan output, the file actions and the safety rules live in [`tripl install`](./cli.md#tripl-install) and are not repeated here. What is left is what the command means for a *deployment*:

- **The health poll goes to your public `--app-url`, never to `localhost`.** If your TLS terminator is not in front of port `8000` yet, that URL cannot answer however healthy the stack is, and the command [times out](./cli.md#waiting-for-health) and exits **1** — with the stack running and nothing rolled back. Bring the proxy up first, or pass `--wait 0` and `curl -fsS http://127.0.0.1:8000/health` from the host.
- **Your data is not in `--dir`.** PostgreSQL lives in the named volume `pgdata18` and uploaded event photos in the named volume `photos`, so backing up the install directory backs up your configuration and none of your data.
- **The `compose.yaml` it writes is the one described [below](#the-compose-stack)**, minus the `mcp` service's `build:` block: a fresh host has no source tree, so `--profile mcp` pulls the published image instead of building it.
- **Re-running converges.** A second run leaves `.env` alone, reports the other two files as `unchanged` or `kept`, and still runs `pull` and `up -d` — which is how you apply an edit to `compose.yaml` or a `compose.override.yaml`.
- **It stops at a running, empty instance.** The owner account and the warehouse connection are browser steps; see [Connecting a warehouse](#connecting-a-warehouse).

### The variables the stack needs

The production stack refuses to start without real secrets. The app's `assert_production_ready()` check (run from the FastAPI lifespan when `DEBUG` is off) rejects an empty or invalid `ENCRYPTION_KEY`, an empty `SECRET_KEY`, insecure session cookies, an unusable CORS origin, and any connection string still carrying the dev-default `tripl:tripl` / `guest:guest` credentials.

Seven variables satisfy it, and that is the whole generated `.env`: `APP_BASE_URL`, `TRIPL_IMAGE` and `TRIPL_VERSION` from your flags and their defaults, plus the four `tripl install` generates — `ENCRYPTION_KEY`, `SECRET_KEY`, `POSTGRES_PASSWORD` and `RABBITMQ_PASSWORD`. What each one *does* is in the [configuration reference](./configuration#identity--secrets); how `install` derives each value, and why the two passwords are hex, is in [the generated `.env`](./cli.md#the-generated-env). No generated value is ever printed — the plan and the `--json` document name the keys and never the values, so if you need to read one, read the `0600` file.

:::danger Treat ENCRYPTION_KEY as irreplaceable
Warehouse credentials and alert-destination secrets are encrypted with `ENCRYPTION_KEY`. If you lose or change it, those stored secrets can no longer be decrypted and must be re-entered. Back it up alongside (but separately from) your PostgreSQL data — a `pg_dump` does **not** contain it. It is also why no `tripl install` flag overwrites an existing `.env`.
:::

### Without the CLI: placing the files and generating the secrets

On a host where you cannot run the CLI at all — no Python 3.12, or no route to PyPI — do the same three things by hand. Put `compose.yaml` and `infra/rabbitmq/rabbitmq.conf` on the host from a checkout of this repository, then generate the secrets — start from the example file and append, since appended lines win over the blanks in `.env.example`:

```bash
cp .env.example .env
{
  echo "ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')"  # Fernet key
  echo "SECRET_KEY=$(openssl rand -hex 48)"
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
  echo "RABBITMQ_PASSWORD=$(openssl rand -hex 24)"
  echo "APP_BASE_URL=https://tripl.example.com"   # your public https URL
  echo "TRIPL_VERSION=1.4.0"                       # pin the release you want
} >> .env
chmod 600 .env
```

The `tr '+/' '-_'` produces the url-safe base64 alphabet Fernet expects; the [configuration reference](./configuration#identity--secrets) gives the equivalent Python one-liners. Then [bring it up](#bring-it-up) as below.

Two differences from the CLI path are worth knowing. `.env.example` is the **backend development** template, so the file you get also carries `localhost` URLs and `PHOTO_*` / `VITE_*` keys that `compose.yaml` never reads — harmless, but do not treat them as live. And `chmod 600` after the fact leaves a window in which the database password was world-readable; the CLI creates the file at `0600` in the `open` call itself.

## URLs, CORS, TLS, and cookies

The production stack hard-codes the security posture that `assert_production_ready()` enforces. In `compose.yaml` these are set for every app/worker container:

- `SESSION_COOKIE_SECURE=true` — cookies are only sent over HTTPS. This is why the stack must sit behind TLS. With it on and no HTTPS in front, browsers will not store the session cookie and login will appear to silently fail.
- `SECURITY_HEADERS_ENABLED=true`, `HSTS_ENABLED=true` (overridable via `HSTS_ENABLED`), `RATE_LIMIT_ENABLED=true`.
- `APP_BASE_URL` drives CORS. When `CORS_ALLOW_ORIGINS` is empty (the default), production derives the allow-list from `APP_BASE_URL`. If both are empty, **no browser can call the API** and startup fails. A wildcard `*` is also rejected in production, because browsers refuse credentialed (cookie) requests against a wildcard origin. Set `APP_BASE_URL` to your exact frontend origin, or set an explicit `CORS_ALLOW_ORIGINS` list if the API is reached from more than one origin.

### Running behind a proxy

The single container is normally the network edge, so the image does **not** pass uvicorn `--forwarded-allow-ips`, and the auth rate limiter keys on the real socket peer (`rate_limit_trust_forwarded_for` defaults to `false`). This is correct when nothing trusted sits in front rewriting client-IP headers.

If you put a trusted reverse proxy or load balancer in front that overwrites `X-Real-IP` / `X-Forwarded-For` with the real client address on every request, set `RATE_LIMIT_TRUST_FORWARDED_FOR=true` and run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>`. Never trust forwarded headers on a directly-exposed API — a raw `X-Forwarded-For` is attacker-controlled and lets a caller rotate it per request to bypass the rate limit.

## The compose stack

`compose.yaml` defines the following services. The four application services all run the same `${TRIPL_IMAGE}:${TRIPL_VERSION}` image; the three infrastructure services and `mcp` run their own:

| Service | Image | Role |
|---|---|---|
| `postgres` | `pgvector/pgvector:0.8.7-pg18-trixie` | Durable application state (also provides pgvector for hybrid search). Data lives in the `pgdata18` volume. Health-checked with `pg_isready`. |
| `rabbitmq` | `rabbitmq:4.3-management` | Celery broker (user `tripl`). Mounts `infra/rabbitmq/rabbitmq.conf`. Health-checked with `rabbitmq-diagnostics ping`. |
| `redis` | `redis:8.8.3-alpine` | Cache only — 256 MB cap, `allkeys-lru`, no persistence. Health-checked with `redis-cli ping`. |
| `migrate` | tripl image | One-shot. Runs `python -m tripl.migrate` (`alembic upgrade head`, then the migrations of any installed extension), then exits. App and workers wait for it to complete successfully. |
| `app` | tripl image | The single API + SPA process on port `8000`. Runs uvicorn with `UVICORN_WORKERS` (default 4). Mounts the `photos` volume at `/app/var/photos`, where the local photo backend keeps uploaded event photos. |
| `celery-worker` | tripl image | Runs `celery -A tripl.worker.celery_app worker`. Executes scans, warehouse queries, monitor evaluation, and alert delivery, plus the daily sweep of orphan photo files. It mounts the same `photos` volume as `app` for that sweep. Its container healthcheck is disabled. |
| `celery-beat` | tripl image | Runs `celery -A tripl.worker.celery_app beat` with the schedule at `/tmp/celerybeat-schedule`. Enqueues periodic jobs. Its container healthcheck is disabled. |
| `mcp` | `${TRIPL_MCP_IMAGE:-ghcr.io/tripl-io/tripl-mcp}:${TRIPL_VERSION}` | **Not started by default** — it is behind `profiles: [mcp]`, so it needs `docker compose --profile mcp up -d`. Serves the [MCP server](../integrate/mcp-server.md) over streamable HTTP for agents. |

In a default run only `app` publishes a port, `8000:8000`. With the `mcp` profile on, `mcp` publishes `127.0.0.1:8765:8765` — bound to loopback on purpose, since it forwards write-capable `tk_w_` keys verbatim and has no auth of its own, so put it behind the same reverse proxy or trusted network as the app before widening that bind.

The `app`, `celery-worker`, and `celery-beat` services all `depends_on` the `migrate` service completing, so workers never start against an un-migrated schema. `app` additionally waits for `postgres`, `rabbitmq`, and `redis` to be healthy.

The frontend is **not** a separate service — `SERVE_FRONTEND=true` and `FRONTEND_DIST_DIR=/app/frontend_dist` are baked into the image, and FastAPI serves the built SPA itself as a low-priority fallback behind the API routes.

The SPA shell is returned for unknown paths only on **navigation requests** — ones whose `Accept` header admits `text/html`. Every other unknown path gets a real `404`, so a broken asset URL or a mistyped `fetch()` fails loudly instead of receiving HTML under a `200`. Worth knowing when probing by hand: `curl https://host/some/deep/route` sends `Accept: */*` and returns `404`, while the same URL in a browser loads the app. Add `-H 'Accept: text/html'` to reproduce what the browser sees. Point uptime checks at `/health`, never at a client-side route.

### Bring it up

[`tripl install`](#install-with-the-cli) runs both of these for you, in the install directory, and waits for `/health` afterwards. By hand, from the directory holding `compose.yaml` and `.env`:

```bash
docker compose pull
docker compose up -d
```

Watch the `migrate` job and then the app come up:

```bash
docker compose logs -f migrate
docker compose logs -f app
```

Note the absence of `-f compose.yaml` and of `--project-directory`: running from inside the directory is what makes compose read `<dir>/.env`, derive the project name from the directory's basename, and still pick up a `compose.override.yaml` you added for TLS or a different port.

## Migrations

Schema upgrades are applied by the dedicated **`migrate`** one-shot, which runs `alembic upgrade head` before `app` or the workers start. Because all of them wait on `migrate` completing successfully, a multi-worker deploy never races the schema upgrade. You do not run migrations by hand in the normal flow — they run automatically on every `docker compose up -d` after a version bump.

:::warning Upgrading an existing database across the migration baseline
The migration history was consolidated into a single baseline revision,
`a1c3e5f7b9d2`. A fresh database can apply that baseline normally. An existing
database must already have **exactly `a1c3e5f7b9d2`** in `alembic_version`
before you deploy a build containing only the baseline. If it is at an older
revision, first deploy a pre-consolidation build and let its full migration
chain reach that head, then take a backup and deploy the baseline build. The
new build cannot traverse revisions that are no longer shipped. Do not manually
stamp an older database as current: stamping changes the marker without
applying the missing schema changes.

Check the database before the version bump:

```bash
docker compose exec -T postgres psql -U tripl -d tripl -Atc 'SELECT version_num FROM alembic_version'
```

The result must be `a1c3e5f7b9d2`. If the table is absent, determine whether
the database is genuinely empty before using the new image; do not treat an
unversioned database containing application tables as a fresh install.

Do not run `alembic downgrade base` on an existing deployment: the single
baseline downgrade removes the entire application schema. Restore a backup
when rolling back a deployed release.
:::

Once the one-shot has run, the applied revision can be confirmed from **Settings → Instance → System** without shelling into a container: the **Schema revision** tile reads the revision this database is stamped with and says whether it matches the head the running build ships. See [System (read-only)](../administer/admin-guide.md#system-read-only).

To run them manually (for example, to inspect output), invoke the same command in a one-off container:

```bash
docker compose run --rm migrate
```

[`tripl upgrade`](./cli.md#tripl-upgrade) deliberately does **not** run this and does not shell out to `alembic` either: the `migrate` one-shot with `condition: service_completed_successfully` is already the race-free mechanism. When an upgrade fails, `docker compose logs migrate` is where the reason is.

## Health check

The `app` container exposes an unauthenticated health probe at **`GET /health`** on port `8000`, returning JSON. The image's Docker `HEALTHCHECK` polls it directly:

```bash
curl -fsS https://tripl.example.com/health
```

The interactive API reference is served at `/docs` (and `/api/v1/*` for the API itself). API routes take precedence over the SPA fallback, so `/health`, `/docs`, and `/metrics` keep their meaning even though the same process serves the frontend.

## Connecting a warehouse

tripl reads from your existing warehouse — it never writes to it. You connect a warehouse **from the UI** after the stack is up, not via environment variables:

1. Open the app at your `APP_BASE_URL` and create the first account on the sign-in page. It becomes the owner — see [Roles & permissions](../administer/admin-guide.md#roles--permissions).
2. Optionally click **Generate demo project** to explore with synthetic data and no warehouse at all.
3. Go to **Settings → Data sources** (under the Workspace group) and add a data source: **ClickHouse**, **BigQuery**, or **PostgreSQL**. The credentials you enter are encrypted at rest with `ENCRYPTION_KEY`.
4. Create a read-only `tk_r_` key under **Settings → API keys** if you want [`tripl doctor`](./cli.md#tripl-doctor) in a cron job. Until that key exists `doctor` cannot run at all: it demands a URL *and* a key before it opens a socket, even though `/health` needs neither.

:::note Step 3 is browser-only by construction, not for want of a CLI
`POST /data-sources` requires an interactive **owner session** and answers `403` to an API key of any scope — see [what a key cannot reach](../administer/admin-guide.md#api-keys--governance). Step 1 *could* be automated and deliberately is not, because it would mean a password on a command line and a `Secure` session cookie that a plain-HTTP first run discards. Between them they are why `tripl install` finishes by [handing you this URL](./cli.md#what-install-deliberately-does-not-do) rather than by finishing the job.
:::

Warehouse queries and scans run on `celery-worker`, so make sure that container has network access to your warehouse. Adapter-specific details live in the [warehouse adapters](https://github.com/tripl-io/tripl/tree/main/backend/src/tripl/core/adapters) source.

## Upgrading

An upgrade is a version bump plus a pull-and-up, and the order matters:

```bash
tripl upgrade --to 1.5.0 --dir /srv/tripl
```

It reads the current pin out of `.env`, refuses a **downgrade** outright, prints the `pg_dump` command and waits for you to acknowledge it, then pulls, moves the pin, restarts and waits for `/health`. Why that order, what it does when the pull fails versus when `up -d` fails, and why a failed `up -d` leaves the new pin in place, are all in [`tripl upgrade`](./cli.md#tripl-upgrade). It never rewrites `compose.yaml`, so a release that changes the stack (the `photos` volume, for one) needs the file updated separately; see the [runbook](./runbook.md#the-photo-volume-release-bring-an-older-composeyaml-up-to-date).

The `migrate` one-shot applies any new Alembic migrations before the new `app` and workers come up, so a rolling deploy never races the upgrade. Pin `TRIPL_VERSION` to an explicit released tag in production rather than tracking `latest`, so upgrades are deliberate and reproducible. Releases are cut from git tags via `bin/release.sh`; the full release machinery is documented in [the release guide](./release.md).

### Without the CLI: upgrading by hand

The comments are the [ordering rule](./cli.md#order-of-operations-and-why), and getting it right is the whole point:

```bash
# 0. Back up first. This applies migrations that cannot be undone,
#    and this dump does NOT contain ENCRYPTION_KEY - keep that separately.
docker compose exec -T postgres pg_dump -U tripl tripl | gzip > tripl-1.4.0.sql.gz

# 1. Pull the new tag first, so a bad tag leaves .env untouched.
TRIPL_VERSION=1.5.0 docker compose pull

# 2. Only now edit .env: TRIPL_VERSION=1.5.0
# 3. Restart. The pin is on disk, so `up` starts the new tag.
docker compose up -d
```

After upgrading, confirm everything is healthy:

```bash
docker compose ps
curl -fsS https://tripl.example.com/health
```

### Browser caching across an upgrade

Every frontend file under `/assets/` carries a content hash, so an upgrade
replaces the whole set. The app serves `index.html` with `Cache-Control:
no-cache` (revalidate every time — the `ETag` keeps that a cheap `304`) and
`/assets/*` with `max-age=31536000, immutable`. That pairing is what stops a
browser from running yesterday's shell against today's chunk names, which
otherwise shows up as intermittent `404`s and
`Failed to fetch dynamically imported module` after a release.

If you put a CDN or reverse proxy in front of tripl, **preserve those headers**.
Overriding them with a blanket TTL on HTML reintroduces exactly this failure,
and it will look random because it only affects clients whose cached copy has
not expired. A tab left open across the upgrade reloads itself once when it
meets a missing chunk, so users do not have to hard-refresh.

## Local preview

The production stack is hardened for HTTPS and will not run comfortably over plain HTTP. To just try tripl on your laptop, use the dev stack, which builds from source with hot-reload and needs no secrets:

```bash
cp .env.example .env
docker compose -f compose.dev.yaml up --watch
```

The dev stack serves the SPA from Vite on `:5173` (proxying to the API on `:8000`). This is **not** a deployment path — use `compose.yaml` for anything real.

## Related

- [Operator CLI](./cli.md) — `tripl install` and `tripl upgrade` in full, plus the read-only diagnostics (`doctor`, `status`, `watch`) for the instance once it is up.
- [Configuration reference](./configuration) — every environment variable and what it does.
- [Administration](../administer/admin-guide.md) — the first owner account, members, roles and API keys.
- [Troubleshooting](../use/troubleshooting) — startup failures, CORS/cookie issues, worker problems.
- [the release guide](./release.md) — cutting and publishing release images.
