---
title: Operations Runbook
sidebar_position: 4
---

# Operations Runbook

Operational procedures for running the self-hosted tripl stack defined in
[`compose.yaml`](https://github.com/vladenisov/tripl/blob/main/compose.yaml):
backups and restore, disaster recovery, horizontal scaling, health checks,
rollback, and post-deploy verification.

For first-time install and the full service/env reference, see
[Deployment](./deployment.md). For symptom-driven debugging, see
[Troubleshooting](../use/troubleshooting.md).

## Stack at a glance

The production stack runs the single published image
(`${TRIPL_IMAGE:-ghcr.io/vladenisov/tripl}:${TRIPL_VERSION:-latest}`) in
several roles — only the command differs:

| Service | Image / command | Persistence | Docker healthcheck |
| --- | --- | --- | --- |
| `postgres` | `pgvector/pgvector:0.8.2-pg18-trixie` | **Durable** — named volume `pgdata18` | `pg_isready -U tripl` |
| `rabbitmq` | `rabbitmq:3.13-management` | **Ephemeral** — no data volume | `rabbitmq-diagnostics -q ping` |
| `redis` | `redis:8.6.2-alpine` (`--maxmemory 256mb --maxmemory-policy allkeys-lru --save ""`) | **Ephemeral** — no volume, no RDB/AOF | `redis-cli ping` |
| `migrate` | `alembic upgrade head` (one-shot) | — | — |
| `app` | API + built SPA on `:8000` | **Durable** — named volume `photos` at `/app/var/photos` (uploaded event photos, local photo backend) | **None** (probe externally — see [Health checks](#health-checks)) |
| `celery-worker` | `celery -A tripl.worker.celery_app worker --loglevel=info` | Same `photos` volume as `app` (for the daily orphan photo sweep) | **Disabled** (`healthcheck.disable: true`) |
| `celery-beat` | `celery -A tripl.worker.celery_app beat --loglevel=info --schedule /tmp/celerybeat-schedule` | — | **Disabled** (`healthcheck.disable: true`) |

`migrate` runs once before `app`, `celery-worker`, and `celery-beat` start: they
all declare `depends_on: migrate: condition: service_completed_successfully`, so
a multi-worker deploy never races the schema upgrade.

:::note
The durable state lives in two named volumes: `pgdata18` on `postgres`
(mounted at `/var/lib/postgresql`, with `PGDATA=/var/lib/postgresql/18/docker`)
and `photos` on `app` (mounted at `/app/var/photos`, the local photo backend's
default root). Redis is a cache and RabbitMQ has no data volume in
`compose.yaml` — both are intentionally ephemeral. Your backup strategy needs to
cover PostgreSQL and, while photos use the local backend, the `photos` volume
([Photo volume backup](#photo-volume-backup)). A photo row whose file is missing
still lists, but its image fails to load.
:::

## PostgreSQL backup & restore

The `postgres` service runs as user `tripl` with database `tripl`. All commands
below run against the running container; run them from the directory containing
`compose.yaml`.

### Logical backup (recommended)

Custom-format dump (compressed, supports selective restore). `-T` disables
pseudo-TTY allocation so the stream pipes cleanly to a file:

```bash
docker compose exec -T postgres \
  pg_dump -U tripl -Fc tripl > tripl-$(date +%F).dump
```

Plain-SQL alternative, gzipped:

```bash
docker compose exec -T postgres \
  pg_dump -U tripl tripl | gzip > tripl-$(date +%F).sql.gz
```

### Restore

For a custom-format (`-Fc`) dump, into the existing `tripl` database, dropping
objects first so the restore is idempotent:

:::warning Migration baseline and older backups
Before restoring a backup with a build that contains the consolidated migration
baseline, check its `alembic_version`. A nonempty database restored at a
revision older than `a1c3e5f7b9d2` must first run the pre-consolidation image's
full migration chain up to that head. Then take a new backup and deploy the
baseline build. A fresh empty database can apply the baseline directly. Do not
stamp an older restored database as current; that skips schema changes.
:::

```bash
docker compose exec -T postgres \
  pg_restore -U tripl -d tripl --clean --if-exists --no-owner < tripl-2026-06-27.dump
```

For a plain-SQL dump:

```bash
gunzip -c tripl-2026-06-27.sql.gz | \
  docker compose exec -T postgres psql -U tripl -d tripl
```

:::warning
Restoring into a live database can conflict with the app and workers. For a
clean restore, stop the application tier first and bring it back afterwards:

```bash
docker compose stop app celery-worker celery-beat
# ... run pg_restore / psql ...
docker compose start app celery-worker celery-beat
```
:::

### Cold volume backup (alternative)

To snapshot the raw `pgdata18` volume instead of a logical dump, stop PostgreSQL
first so the data files are consistent, then archive the volume:

```bash
docker compose stop postgres
docker run --rm \
  -v tripl_pgdata18:/var/lib/postgresql \
  -v "$PWD":/backup alpine \
  tar czf /backup/pgdata18-$(date +%F).tar.gz -C /var/lib/postgresql .
docker compose start postgres
```

The Compose project prefixes the volume name (commonly `tripl_pgdata18`); confirm
with `docker volume ls`.

## Photo volume backup

With the local photo backend (the default; see
[Event photo storage](configuration.md#event-photo-storage)), each uploaded
event photo is a file in the `photos` volume, while its row, including the key
that locates the file, is in PostgreSQL. Back both up, and take **PostgreSQL
first, the files second** — the order matters, and it is the opposite of the
intuitive one. A database restored without its files lists photos whose images
fail to load; a file no row points at is inert and costs nothing but disk.

An upload writes its file and only then commits the row, so every row in a dump
names a file that was complete before the dump was taken — and therefore before
an archive that starts afterwards reads it. An upload landing during the
archive is either missed or caught half-written, and either way no row in the
dump refers to it. Reverse the order and that stops holding: the dump taken
last would carry a row for a file the archive caught truncated.

A file is written once under a fresh random name and never rewritten, so the
archive cannot catch an *edit* — but the local backend writes straight to the
final path, so it can catch a write in progress. If you would rather have no
partial member in the archive at all, stop `app` for it, or take a filesystem
snapshot (LVM, ZFS, or the volume driver's own) and tar that:

```bash
docker run --rm \
  -v tripl_photos:/data:ro \
  -v "$PWD":/backup alpine \
  tar czf /backup/photos-$(date +%F).tar.gz -C /data .
```

To restore, stop `app` and `celery-worker`, unpack into the volume, and hand it
back to the image's `app` user (uid 1000), which must be able to write there:

```bash
docker compose stop app celery-worker
docker run --rm \
  -v tripl_photos:/data \
  -v "$PWD":/backup alpine \
  sh -c 'tar xzf /backup/photos-2026-06-27.tar.gz -C /data && chown -R 1000:1000 /data'
docker compose start app celery-worker
```

:::warning Restore the database first
`celery-worker` runs a daily sweep that deletes photo files no row in the
database references, once they are older than `PHOTO_ORPHAN_SWEEP_GRACE_HOURS`
(default 24; see [Event photo storage](configuration.md#event-photo-storage)).
Do not let the worker run with the `photos` volume mounted against an empty or
half-restored database. The sweep would read every file as an orphan. Restore
PostgreSQL before starting `celery-worker`, as the recovery procedure below
does. Files that the restored dump does not reference (uploads made after the
dump) are orphans by design, and the next sweep deletes them.
:::

As with `pgdata18`, confirm the prefixed volume name with `docker volume ls`.
With the Google Cloud Storage backend new files go to the bucket instead.

## Disaster recovery

Recovery hinges on the durable/ephemeral split:

- **PostgreSQL (`pgdata18`) — durable, must be restored.** This holds tracking
  plans, data sources, scan history, metrics, alerts, and user accounts. Restore
  it from your latest dump (above) on a fresh host before starting the app tier.
- **Photos (`photos`) — durable, restore alongside PostgreSQL** when photos use
  the local backend ([Photo volume backup](#photo-volume-backup)). A photo row
  whose file is missing still lists, but its image fails to load.
- **Redis — ephemeral cache, rebuilds itself.** It runs with `--save ""` and no
  volume, so a restart starts empty. The app degrades gracefully: reads fall
  through to PostgreSQL and the cache repopulates. (In `compose.yaml`,
  `REDIS_URL` points at the `redis` service; an empty `REDIS_URL` disables
  caching entirely, with every read going to the DB.)
- **RabbitMQ — ephemeral broker.** With no data volume, queued messages do not
  survive a broker restart. Celery is configured with `task_acks_late=True` and
  `task_reject_on_worker_lost=True` (see
  [`celery_app.py`](https://github.com/vladenisov/tripl/blob/main/backend/src/tripl/worker/celery_app.py)),
  which re-queues a task when a **worker** crashes mid-execution — but that does
  not protect messages already sitting in the broker if **RabbitMQ itself** is
  lost. `celery-beat` resumes recurring work at the next scheduled time:
  metric checks and stranded-delivery requeue run every 5 minutes, while
  maintenance runs at 03:00, 04:00, and 05:00 UTC, and the weekly plan digest
  runs Monday at 08:00 UTC.
  A missed tick is **not** replayed. If the broker was down during a scheduled
  run, check the affected work after it recovers.
- **`celery-beat` schedule file** lives at `/tmp/celerybeat-schedule` inside the
  beat container and is regenerated on start — nothing to back up.

### Recovery procedure (fresh host)

```bash
# 1. Restore .env (secrets: POSTGRES_PASSWORD, RABBITMQ_PASSWORD,
#    ENCRYPTION_KEY, SECRET_KEY, APP_BASE_URL) and compose.yaml.
# 2. Pull the same image tag that produced the backup.
docker compose pull
# 3. Bring up only PostgreSQL and restore the dump.
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U tripl -d tripl --clean --if-exists --no-owner < tripl-LATEST.dump
# 4. Local photo backend: create the remaining containers and volumes without
#    starting them, then unpack the photos archive (see Photo volume backup).
docker compose up --no-start
# 5. Start the rest (migrate runs alembic upgrade head, then app + workers).
docker compose up -d
```

:::danger
`ENCRYPTION_KEY` is the Fernet key that decrypts stored data-source and
alert-destination secrets. If it is lost, those encrypted columns are
**unrecoverable** even with a perfect database backup. Store it with the same
care as the database backups themselves.
:::

## Horizontal scaling

### Scaling Celery workers

Each worker process opens one shared sync SQLAlchemy engine + connection pool on
first use (see
[`worker/db.py`](https://github.com/vladenisov/tripl/blob/main/backend/src/tripl/worker/db.py)),
and runs with `worker_prefetch_multiplier=1` so one slow task can't hoard the
queue while peers idle. Scale out by adding replicas:

```bash
docker compose up -d --scale celery-worker=3
```

Account for the extra database connections (each worker process holds a pool)
when sizing PostgreSQL `max_connections`. Long tasks are bounded by a 55-minute
soft limit (`SoftTimeLimitExceeded`, allows cleanup) and a 60-minute hard limit.

### Scaling the app tier — rate-limit caveat

The auth rate limiter (`/auth/login`, `/auth/register`) is a token bucket
keyed on `(client_ip, route)`, stored in Redis when `REDIS_URL` is set — see
[`middleware/rate_limit.py`](https://github.com/vladenisov/tripl/blob/main/backend/src/tripl/middleware/rate_limit.py).
Defaults are 5 login attempts/minute and 3 registrations/hour
(`rate_limit_login_per_minute`, `rate_limit_register_per_hour`).

:::warning Without Redis, limits do not aggregate
With Redis, every worker and every replica pointed at the same Redis draws on
one bucket per client, so the limit holds however far you scale. Without
`REDIS_URL` — or while Redis is unreachable, when each worker falls back to its
own in-memory bucket — running **N** app replicas (or multiple Uvicorn workers)
multiplies the effective limit: with `--scale app=N` the practical login
ceiling is roughly `N × 5/min`.
:::

If you do put a trusted proxy in front, set `RATE_LIMIT_TRUST_FORWARDED_FOR=true`
(default `false`) so the limiter keys on the real client IP. It prefers
`X-Real-IP`, falling back to the leftmost `X-Forwarded-For` entry. Enable this
**only** behind a proxy that overwrites `X-Real-IP` on every request — a raw
`X-Forwarded-For` on a directly-exposed API is attacker-controlled and lets a
caller rotate the header to land each request in a fresh bucket. When the app is
the edge (the default single-container deploy), leave it at `false` so the
direct socket peer (`request.client.host`) is used.

## Health checks

The app exposes `GET /health` — an unauthenticated liveness + DB-reachability
probe. It runs `SELECT 1` against PostgreSQL with a 1-second timeout:

- **Healthy:** HTTP `200` with body `{"status":"ok"}`.
- **DB unreachable:** HTTP `503` with body `{"status":"error","component":"database"}`.

```bash
curl -fsS http://localhost:8000/health
# {"status":"ok"}
```

:::note
The `app` service has **no Docker healthcheck** in `compose.yaml`, and the
`celery-worker` / `celery-beat` healthchecks are explicitly disabled. Wire
`GET /health` into your external monitor or orchestrator probe rather than
relying on `docker compose ps` health status for the app. `/health`,
`/api/v1/*`, `/metrics`, and `/docs` take precedence over the SPA fallback, so
the probe path is always served by the API.
:::

Worker and beat liveness are best checked from logs and broker state:

```bash
docker compose logs --tail=50 celery-worker
docker compose logs --tail=50 celery-beat
```

The Prometheus `/metrics` endpoint is only mounted when
`PROMETHEUS_METRICS_ENABLED=true` (off by default); expose it behind an
internal-only path. The Compose stack shares a Prometheus multiprocess
directory between the API and Celery worker and clears its old metric files
before startup, so the endpoint includes worker-side scan, anomaly, drift,
alert, and Celery task measurements. If you deploy the processes separately,
provide the same writable `PROMETHEUS_MULTIPROC_DIR` to both and clear stale
files at deployment startup.

## Rollback / downgrade

Releases are image-tagged. To roll back the application, pin `TRIPL_VERSION` to a
prior released tag in `.env`, pull, and recreate:

```bash
# .env
TRIPL_VERSION=1.3.0

docker compose pull
docker compose up -d
```

:::danger Migrations are forward-only
The `migrate` one-shot runs `alembic upgrade head` — it **never downgrades**.
Pulling an older image does **not** revert schema changes that a newer release
applied. If the version you are rolling back to predates a migration, the old
code may be incompatible with the upgraded schema.

If you must reverse a schema change **after** the baseline, run the Alembic
downgrade explicitly with a one-off container **before** starting the older app
(override the `migrate` service's command):

```bash
docker compose run --rm migrate alembic downgrade <target_revision>
```

Take a fresh backup first (see [Backup & restore](#postgresql-backup--restore))
— for non-trivial rollbacks, restoring a pre-upgrade dump is often safer than a
downgrade migration. Validate the rollback in staging where possible.
The baseline has no historical predecessor in this build: `downgrade base`
would remove the entire application schema. Restore a backup to return to a
pre-baseline state.
:::

## Post-deploy verification

After any `docker compose up -d` (deploy, rollback, or recovery):

1. **Migration completed.** The one-shot must have exited cleanly:

   ```bash
   docker compose ps -a migrate          # State should be "Exited (0)"
   docker compose logs migrate           # ends with the upgrade head output
   ```

   Then confirm it from the database rather than from the compose file:
   **Settings → Instance → System** shows the revision this database is actually
   stamped with and whether it equals the head this build ships. The two checks
   above are an *inference* — the app started, so the one-shot it waits on must
   have succeeded — and they are only available where that compose file is what
   started the instance. The **Schema revision** tile is an *observation*, and it
   still holds on a hand-rolled deploy that never ran `alembic upgrade head`.
   That matters because a constraint-only migration changes nothing else a probe
   can see: a skipped upgrade looks exactly like a correct one until something
   writes. See [System (read-only)](../administer/admin-guide.md#system-read-only)
   for the three states the tile reports.

2. **Core services up and healthy.**

   ```bash
   docker compose ps
   # postgres / rabbitmq / redis: Up (healthy)
   # app / celery-worker / celery-beat: Up
   ```

3. **API health probe passes.**

   ```bash
   curl -fsS http://localhost:8000/health   # {"status":"ok"}
   ```

4. **Workers are processing.** Confirm the worker connected to the broker and
   beat is emitting ticks:

   ```bash
   docker compose logs --tail=30 celery-worker   # "celery@... ready"
   docker compose logs --tail=30 celery-beat     # "Scheduler: Sending due task ..."
   ```

5. **App logs are clean.** No repeated tracebacks or production-startup-check
   failures (`assert_production_ready` refuses to boot with missing secrets or
   dev-default credentials):

   ```bash
   docker compose logs --tail=50 app
   ```

If any step fails, see [Troubleshooting](../use/troubleshooting.md) for
symptom-driven diagnosis, or roll back per the section above.

### The photo-volume release: bring an older `compose.yaml` up to date

:::warning `tripl upgrade` does not rewrite `compose.yaml`
Before this release the image could not create the local photo backend's
directory, so every photo upload failed unless photos went to Google Cloud
Storage. Now local uploads succeed, and `compose.yaml` mounts the named
volume `photos` at `/app/var/photos` so they survive a redeploy. `tripl upgrade`
only moves the version pin, so a stack installed earlier keeps a `compose.yaml`
without that volume: its uploads land in the container and are lost the next
time the container is recreated. Before anyone uploads, re-run
[`tripl install`](./cli.md#tripl-install) from a CLI that ships this release,
with `--force` (the file it replaces is kept as `compose.yaml.bak.<timestamp>`,
and `--force` never touches `.env`), or add the `volumes:` entry under `app` and the
top-level `photos:` by hand. After `docker compose up -d`, `docker volume ls`
lists the prefixed volume (commonly `tripl_photos`).
:::

### The scan-identity release: look for events tagged `duplicate-identity`

:::note This upgrade may tag events, and deletes none
Before the baseline squash, the migration that made a scan identity unique
(one event per identity per event type) first repaired any events that already
shared one.
Per identity it keeps the row scan traffic most recently landed on — the same
choice a scan makes — and leaves every other row in place with its identity
suffixed ` #duplicate-<event id>` and the tag `duplicate-identity`. Nothing is
deleted or merged. After the deploy, filter **Plan › Events** by that tag on
each project (and on an open branch, which copied the pair): an empty result
means the database held no duplicates. For each tagged event decide whether to
delete it or keep it as history; either way it no longer receives scan data,
and the untagged twin does.
:::

### One-off: rebuild the search index after the ranking release

:::warning Required once, per project **and** per plan branch
The release that fixed search ranking changed **what text is indexed** for a
document — harvested field values left a variable's keywords, and every
snake_case / dotted identifier gained a spaced alias. The migration that ships
with it re-tokenizes the text already stored, but it does **not** rebuild that
text. Until a branch is rebuilt, its documents are ranked on the old text, and a
branch where some rows have been rebuilt and others have not is ranked on both
at once.
:::

Most branches repair themselves: **any** write to an event, event type, field,
meta field, variable, relation, metric or fact table rebuilds that branch's
whole index, as do a plan-branch merge, a demo reset, and the scan/catalog
refresh the Celery worker runs. An actively used project needs nothing from you.

A branch that has never been indexed **at all** — a plan branch just created, or
a project nobody has searched yet — is picked up by the read path rather than by
a write: the first search of it enqueues
`tripl.worker.tasks.search.reindex_search_branch` and answers with what is
stored, which for that branch is nothing. An empty first search followed by a
populated second one is expected, not a fault; the search does not block while
the rebuild runs. Reach for the manual rebuild below only if it stays empty,
which means the enqueue never reached a worker — check the broker and the
`celery-worker` logs — because the read path remembers that it asked and will
not ask again for the life of that API process.

Branches nobody writes to — archived plan branches, projects kept for reference
— used to keep the old documents indefinitely. They no longer do when the change
was to how documents are BUILT: each row carries the generation of the builders
that wrote it, and the `reindex-stale-search-documents` beat task rebuilds a
couple of lagging branches every ten minutes until the whole instance is current.
A ten-branch instance converts inside an hour. Rows whose text did not actually
change keep their vector and their embedding and only have their stamp
corrected, so the sweep costs nothing at your embedding provider beyond the
documents that genuinely moved.

That covers builder changes only. A migration that rewrites stored vectors
without changing document text — a text-search configuration change, for
instance — leaves the stamp alone by design, and so does an index you want
rebuilt for any other reason. Rebuild those explicitly:

```bash
# Editor role or above. Once per project AND per plan branch.
# $TRIPL_API_KEY is a write-scoped personal API key (tk_w_…), see Security.
curl -fsS -X POST \
  -H "Authorization: Bearer $TRIPL_API_KEY" \
  "http://localhost:8000/api/v1/projects/<slug>/search/reindex?branch=<branch_id>"
# -> {"documents_indexed": N, "embeddings_scheduled": true|false}
```

The parameter is `branch`, not `branch_id`. An unrecognised query parameter is
not an error — it is ignored, and the request rebuilds **main** instead of the
branch you named, reporting success either way. If you are repairing a specific
plan branch, check `documents_indexed` against that branch's size before
believing it.

`embeddings_scheduled` reports whether a refresh task was actually handed to the
broker, so `false` while `SEARCH_EMBEDDINGS_ENABLED=true` means the enqueue
failed and the rebuilt rows are sitting at `embedding_status='pending'` with
nothing coming for them.

This is intentionally **not** done for you by the migration. Rebuilding drops
and re-inserts the affected rows, which discards their stored embeddings — with
`SEARCH_EMBEDDINGS_ENABLED=true` that means the catalog is re-embedded against
your provider, at your cost. Deciding when to pay that is an operator call, not
a side effect of `alembic upgrade head`. With embeddings disabled (the default)
a rebuild is free apart from the CPU it takes.

Verify by searching for an entity whose name contains an underscore, using a
space instead (`screen home` for `screen_home`): the entity itself should come
back first rather than the variables that merely mention it.

### The surface-form release: check BEFORE you deploy, no rebuild after

The release that indexes a word's **surface form beside its stem** (so that
`экран` and `экране` reach the same documents — Snowball over-stems some forms
of a word onto a lexeme its other forms never produce) rebuilds every stored
`text_vector` inside its migration. Unlike the ranking release above it does
**not** change what text a document contains, so no `search/reindex` call is
needed anywhere, archived branches included, and no embedding is discarded or
re-billed.

What it does need is one check *before* the deploy, because a `tsvector` cannot
exceed 1 MB and every document's vector roughly doubles. If any row would cross
that limit the migration aborts — with the API entrypoint running
`alembic upgrade head` before uvicorn, that is a failed start, not a warning.

**Step 1 — triage the whole table (cheap, cannot fail).** A tsvector's lexemes
are substrings of its input and its position list is bounded by the token count,
so both legs together stay under 1 MB for any document text below ~250 KB. This
finds the rows that need the exact check:

```bash
docker compose exec -T postgres psql -U tripl -d tripl -c "
SELECT count(*)                                   AS documents,
       count(*) FILTER (WHERE octet_length(txt) > 250000) AS rows_to_inspect,
       pg_size_pretty(max(octet_length(txt))::bigint)     AS largest_document
FROM (
    SELECT concat_ws(' ', title, subtitle, body, keywords) AS txt
    FROM search_documents
) s;"
# rows_to_inspect = 0  ->  no row can reach the 1 MB cap. You are done; deploy.
```

**Step 2 — only if `rows_to_inspect > 0`: measure those rows exactly.**

Do **not** reach for `SELECT length((to_tsvector(...) || to_tsvector(...))::text)
FROM search_documents`. That is the obvious formulation and it cannot report the
condition it is looking for: building an oversized `tsvector` is precisely what
raises `string is too long for tsvector`, so the query aborts on the first
offending row with the same error the migration would have raised. It tells you
nothing about how many rows are affected or which ones, and an operator who runs
it sees a broken check rather than an answer.

This does the same measurement per row and traps that error instead of
propagating it, so every offending row is named. It is read-only — no `CREATE`,
no writes, nothing to clean up:

```bash
docker compose exec -T postgres psql -U tripl -d tripl -c "
DO \$\$
DECLARE
    doc record;
    bytes int;
    offenders int := 0;
BEGIN
    FOR doc IN
        SELECT id, entity_type, entity_id,
               concat_ws(' ', title, subtitle, body, keywords) AS txt
        FROM search_documents
        WHERE octet_length(concat_ws(' ', title, subtitle, body, keywords)) > 250000
    LOOP
        BEGIN
            bytes := pg_column_size(
                to_tsvector('tripl_search', doc.txt)
                || to_tsvector('simple', unaccent(doc.txt))
            );
            IF bytes > 1000000 THEN
                offenders := offenders + 1;
                RAISE NOTICE 'OVER LIMIT: % bytes  doc=%  %/%',
                    bytes, doc.id, doc.entity_type, doc.entity_id;
            END IF;
        EXCEPTION WHEN program_limit_exceeded THEN
            offenders := offenders + 1;
            RAISE NOTICE 'OVER LIMIT: % (doc=%  %/%)',
                SQLERRM, doc.id, doc.entity_type, doc.entity_id;
        END;
    END LOOP;
    RAISE NOTICE 'check complete: % row(s) over the 1 MB tsvector cap', offenders;
END \$\$;"
# Expect: "check complete: 0 row(s) over the 1 MB tsvector cap".
# Anything above 0 must be resolved before deploying: the offending document's
# body is a harvested-value blob and needs trimming at the source. Deploying
# without resolving it is a failed container start, not a degraded search.
```

Three notes on that block. `program_limit_exceeded` (SQLSTATE 54000) is the error
class Postgres raises for `string is too long for tsvector`; any *other* error
still propagates and aborts, which is what you want — a check that swallowed
everything would be as useless as one that aborts on everything. The `EXCEPTION`
branch is the authoritative signal: a row that raises there is a row the
migration will fail on, while the `bytes > 1000000` branch is the near-boundary
warning, so treat anything it reports as needing the same trimming.
`to_tsvector('simple', unaccent(…))` stands in for `tripl_search_surface`, which
does not exist yet on the database you are checking; it is the same dictionary
chain the migration installs, so the byte counts match to within the handful of
non-word tokens the two treat differently.

Afterwards, a full-table `UPDATE` has left dead tuples and a bloated GIN index.
Autovacuum will get there; if search feels slow immediately after the deploy,
hurry it along:

```bash
docker compose exec -T postgres psql -U tripl -d tripl -c \
  "VACUUM (ANALYZE) search_documents;"
```

Verify with a Russian noun in two cases — but not just any two. Snowball puts
`экран`, `экрана` and `экраны` in one class and `экране` in another, so
`экран`/`экрана` returned the same entities before this release as well and
proves nothing. Use a pair that actually straddles the split:

- `улов` and `уловы`
- `архив` and `архивы`
- `экран` and `экране`

Both spellings of a pair should return the same entities. Before this release one
of the two returned nothing at all.

### The bucket-alignment release: weekly scans re-phase to Monday

:::warning Weekly (`1w`) scans only — nothing changes for `15m`, `1h`, `6h` or `1d`
The warehouses always grouped weeks from Monday, but the worker measured the
**window** it queried from a 2000-01-01 anchor, which is a *Saturday*. The two
grids did not line up, and only weeks are affected — every shorter interval
divides a day evenly, so its window boundaries landed on bucket boundaries from
either anchor. This release measures the window from the same Monday origin the
buckets use, so a weekly window now opens and closes on a Monday.

**What to expect after the deploy, without doing anything.**

- **One re-phasing gap.** The first weekly collection waits for the next Monday
  boundary instead of the next Saturday one, so it lands about **two days later**
  than the old cadence implied. After that it is weekly again, on Mondays. (A
  weekly scan that has stored nothing yet becomes due at the next Monday instead,
  which can be sooner.)
- **The newest weekly point stops reading low.** A run used to end on a Saturday,
  so the most recent weekly point it wrote covered Monday through Friday — five
  days of seven — and was only completed by the following week's run. A run now
  ends on a Monday, so every weekly point it writes covers its whole week. The
  first post-deploy run re-collects roughly the last three weekly points and
  fills in the partial one; expect that bar to step **up**, which is the
  correction, not a traffic spike.
- **A weekly scan that sets Replay chunk size needs a replay.** Chunk boundaries
  were on the same misaligned grid, and a chunk replaces the points inside its
  own window, so each weekly point kept only the part of the week that fell in
  the last chunk touching it. Points written that way are understated and the
  scheduled run above only reaches the newest few. Fill the rest with **Replay a
  period…** (in the scan page header) over the period you care about —
  chunking is Monday-aligned now, so the replay writes whole weeks.
- **Signals on the corrected points.** The detector scores each run against the
  points in storage at the time, so a corrected weekly point can read as a jump
  against uncorrected history behind it. Replaying that history removes the
  cause.
:::

:::note The replay API refuses a period ending "now"
In the same release, `POST /projects/{slug}/scans/{scan_id}/metrics/replay`
answers **`400`** when `time_to` falls inside the interval that is still filling.
It previously answered `201` and then produced a failed run. The browser dialog
already seeds a period that ends on the last complete bucket, so this only
affects a non-UI caller that posts a window ending at the current instant: floor
that end to the scan's own interval. See
[Replaying Metrics](../integrate/agent-api-guide.md#replaying-metrics).
:::
