---
title: Operator CLI
sidebar_position: 5
---

# Operator CLI

`tripl` is a small command-line client for a tripl instance. Most of it talks to
a **running instance** over HTTP; two commands instead act on a **directory and
the local Docker daemon**, and those are the ones that bring an instance into
existence in the first place.

Three commands ask a question about the instance as a whole and change nothing:

- **`tripl doctor`** — runs six diagnostic checks and tells you what is broken,
  why, and what to do about it. Exits non-zero when something is wrong.
- **`tripl status`** — a quick live view of every project: events, scan configs,
  open signals, firing monitors, reconciliation coverage. Always exits 0.
- **`tripl watch`** — follow mode. Prints what changes while you watch: replay
  chunk progress, jobs starting and finishing, signals opening, alert deliveries
  failing. Runs until you stop it. Never reports a verdict.

Two more act on a **class of objects** and are spelled `<plural-noun> <verb>`:

- **`tripl scans`** — `list` the scan configurations, print one's `jobs`, `run`
  one now, `cancel` an active job.
- **`tripl drifts`** — `list` schema drifts, `dismiss` one, `reopen` one.

One more records a single fact about a project and is one word:

- **`tripl annotate`** — post a deploy or release marker onto every monitoring
  (Volume tab) chart in a project, from a CI step. See [`tripl annotate`](#tripl-annotate).

`scans run`, `scans cancel`, `drifts dismiss`, `drifts reopen` and `annotate`
are the CLI's **only mutating commands**. Read [Write safety](#write-safety) before you use one. Every other
command that talks to an instance is read-only, and a `tk_r_` key is enough for
all of them.

Two more are read-only in full, and answer questions about the **content**
rather than the machinery:

- **`tripl events`** — `list` the catalog with the API's own filters, `show` one
  event with its field values resolved to field names.
- **`tripl plan`** — the shape events are declared to have: `types`, one type's
  `fields`, the documented `variables`, the plan `branches`, and `search` across
  all of it.

Both read one project at a time, so both need `--project SLUG` exactly once, and
every verb but `plan branches` takes `--branch` to read a plan branch instead of
the live main plan.

Two act on a **host**, not on an instance — no URL, no API key, no HTTP except a
`/health` poll at the end:

- **`tripl install`** — writes `compose.yaml`, `infra/rabbitmq/rabbitmq.conf` and
  a generated `0600` `.env` into a directory, runs `docker compose pull` and
  `docker compose up -d` in it, and waits until the instance answers. This is the
  executable form of [Self-hosting & Deployment](./deployment.md).
- **`tripl upgrade`** — moves an installed stack to a new image tag: pull, move
  the `TRIPL_VERSION` pin, restart, wait.

Everything that talks to an instance is a pure HTTP client of the
[`/api/v1`](../integrate/agent-api-guide.md) surface, exactly like the
[MCP server](../integrate/mcp-server.md) — it imports no backend code and never
touches the database. Since the two tools now
[share one request layer](#one-request-layer-shared-with-the-mcp-server), a path
or a response projection is defined in exactly one place for both. They also
read **the same two environment variables**, so a shell configured for one is
configured for the other. `install` and `upgrade` are the exception in both
directions: they run a subprocess (`docker`), and they read neither variable.

:::note What this page is for
`tripl doctor` exists because of a four-day incident in which a scan config had
been failing behind a generic *"Scan failed due to an internal error"*, events'
`last_seen_at` had frozen with no visible cause, an accepted schema drift had
silently deleted a field definition, and the scheduler's retry backoff looked
like a hung worker. Every one of those was visible through read-only REST calls.
This is the one invocation that surfaces them.
:::

## Install

The only runtime dependency is `httpx`. **Python 3.12 or newer is required for
the `pip` path only** — every `uv`-driven form below (`uvx`, and the
`uv run --project` checkout form) provisions a suitable interpreter itself, so a
host carrying `uv` and no Python at all still runs the CLI.

**From PyPI.** `tripl` is published, so this is the ordinary path:

```bash
uvx tripl doctor      # run it without installing anything permanently
pip install tripl     # or put it on PATH for good
```

**From a checkout.** The form to use when you are working on the CLI itself:

```bash
git clone https://github.com/vladenisov/tripl.git
uv run --project tripl/cli tripl --version
# tripl 0.1.0
```

**From git, without a checkout.** How you get a revision that is not released
yet. Both forms below resolve, but neither is exercised by CI:

```bash
uvx --from 'git+https://github.com/vladenisov/tripl.git#subdirectory=cli' tripl doctor

pip install 'git+https://github.com/vladenisov/tripl.git#subdirectory=cli'
```

:::note "Install the CLI" and "`tripl install`" are different things
This section is about getting the `tripl` **command** onto your machine.
[`tripl install`](#tripl-install) is a *subcommand* of it that provisions a
**tripl server** on a host. You need the first before you can run the second —
`uvx tripl install ...` on the deploy host does both in one step.
:::

:::tip Distribution name vs import name
The distribution and the console script are both `tripl`; the *import* package
is `tripl_cli`. That is deliberate — the service's own source package is
`backend/src/tripl/`, so a distribution installing an importable `tripl` would
shadow it in any environment holding both.

The service is packaged as a *separate* distribution, `tripl-server`, and is
never published to an index — it ships as a container image. So `tripl` on PyPI
means this CLI and only this CLI.
:::

## Configuration

Two values are needed: the instance URL and an API key. Each is resolved
**independently**, through three layers, highest precedence first:

| # | Layer | Instance URL | API key |
|---|-------|--------------|---------|
| 1 | Command-line flag | `--url` (alias `--base-url`) | `--api-key` |
| 2 | Environment | `TRIPL_BASE_URL` | `TRIPL_API_KEY` |
| 3 | Config file | `base_url` | `api_key` |

Per field, not per source: `tripl --url https://staging.example.com doctor` with
the key still coming from the config file works exactly as you would expect. An
empty string counts as *absent* at every layer, so a Compose-materialised
`TRIPL_BASE_URL=""` falls through to the file instead of shadowing it with
nothing.

```bash
export TRIPL_BASE_URL=https://tripl.example.com
export TRIPL_API_KEY=tk_r_...
tripl doctor
```

:::note There is no `TRIPL_URL`
Only `TRIPL_BASE_URL` is read — the same variable the
[MCP server](../integrate/mcp-server.md#configuration) uses. `TRIPL_URL` is
*detected* purely so that, when it is set and `TRIPL_BASE_URL` is not, the error
names the right variable instead of shrugging.
:::

A URL pasted from the browser with `/api/v1` already on the end is trimmed
rather than doubled, and a bare host with no scheme gets a corrected suggestion
in the error.

### Config file

| Platform | Path |
|----------|------|
| Linux / BSD / macOS | `$XDG_CONFIG_HOME/tripl/config.toml`, else `~/.config/tripl/config.toml` |
| Windows | `%APPDATA%\tripl\config.toml`, else the `~/.config` fallback |

`XDG_CONFIG_HOME` is honoured on every platform, but only when it is an absolute
path. macOS uses `~/.config` rather than `~/Library/Application Support` because
this is a file an operator opens in `$EDITOR`, the same choice `git`, `gh`,
`aws`, `kubectl` and `docker` make.

```toml
# ~/.config/tripl/config.toml
base_url = "https://tripl.example.com"
api_key  = "tk_r_2f9c..."
```

Unknown keys and unknown tables are **ignored**, so an older CLI keeps working
against a file written by a newer one. `--config PATH` overrides discovery
entirely: a missing *default* file is fine, a missing `--config` path is an
error.

:::warning Permissions on a file holding a key
On POSIX, a config file that contains an `api_key` and is readable by other
users produces one warning on stderr. Fix it with `chmod 600
~/.config/tripl/config.toml`. Passing `--api-key` on the command line is worse
still — it is visible to every user on the box through `ps(1)` and lands in your
shell history. Prefer the environment variable or the config file.
:::

### Which key to use

**A read-only `tk_r_` key is enough for `doctor`, `status`, `watch`, `scans
list`, `scans jobs` and `drifts list`, and it should be your default.** Those
six issue nothing but `GET`, so a write key buys them nothing and risks
everything.

Five verbs mutate the instance and need a **`tk_w_` key backed by a user with
the editor or owner role**: `scans run`, `scans cancel`, `drifts dismiss`,
`drifts reopen` and `annotate`.
Give them a key of their own rather than promoting the one in your cron job —
see [Write safety](#write-safety).

**`tripl install` and `tripl upgrade` need no key and no URL at all.** They act
on a directory and the local Docker daemon, so passing `--url` or `--api-key`
*explicitly* is **exit 2** rather than a silently ignored flag. An ambient
`TRIPL_BASE_URL` exported for `tripl doctor` does not trip that — only a flag you
typed does.

Create keys in the app at **Settings → API keys** (see
[API keys & governance](../administer/admin-guide.md#api-keys--governance)). The
role behind the key still matters for two things:

- **Owner role** — `data_source.last_test_message` is redacted for anyone below
  owner, so the *text* of a failed connection probe is only visible to an
  owner's key. doctor says so explicitly rather than reporting "no message".
- **Project-bound keys** — a key fenced to one project cannot list projects at
  all. Pass `--project <slug>`; see [below](#project-scoped-keys).

## `tripl doctor`

```
usage: tripl doctor [-h] [--url URL] [--api-key KEY] [--config PATH]
                    [--project SLUG] [--include-demo] [--json] [--strict]
                    [--timeout SECONDS] [--max-event-types N]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | Check only this project. Repeatable. Required for a project-scoped key. |
| `--include-demo` | Also check demo projects, which are excluded by default. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--strict` | Exit 3 on warnings too — never on skipped checks. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |
| `--max-event-types N` | Event-type budget for the drift scan, default `200`, range 1–10000. |

A healthy instance:

```text
tripl doctor - https://tripl.example.com (from $TRIPL_BASE_URL)

PASS  connectivity  Reached https://tripl.example.com (from $TRIPL_BASE_URL); the API and its database are up.
PASS  auth          The API key authenticates as an instance-wide key (role: owner).
PASS  projects      1 project selected.
PASS  data_sources  1 data source(s) referenced by the selected scan configs; none reports a failed probe.
PASS  scans         1 scheduled scan configs are collecting on time.
PASS  drifts        1 event type(s) examined; no untriaged schema drift.

6 checks: 6 pass. No problems found.
```

An instance with the incident on it:

```text
tripl doctor - https://tripl.example.com (from $TRIPL_BASE_URL)

PASS  connectivity  Reached https://tripl.example.com (from $TRIPL_BASE_URL); the API and its database are up.
PASS  auth          The API key authenticates as an instance-wide key (role: owner).
PASS  projects      1 project selected.
FAIL  data_sources  1 referenced data source(s); see below.
      - fail: data_source_probe_failed 'warehouse-prod'
        Data source 'warehouse-prod' (used by scan config 'prod events', 'checkout funnel') last failed its connection test at 2026-07-29T19:08:09Z: 'FATAL: password authentication failed for user "tripl"'.
FAIL  scans         1 of 2 scheduled scan configs is not collecting.
      - fail: scan_config_failing [prod] 'prod events'
        Scan config 'prod events' (1h) has failed 5 consecutive scheduled runs since 2026-07-31T14:08:09Z. Last error: 'Scan failed due to an internal error.' - that is the backend's generic fallback, not the real cause, so the cause is in the worker log for job job-0.
      - warn: scan_backoff_active [prod] 'prod events'
        The scheduler has deliberately deferred the next attempt to not before 2026-07-31T22:08:09Z (about 4h after the last failure): 3 or more consecutive failures trigger a backoff, so the worker is not stuck.
WARN  drifts        1 event type(s) examined; see below.
      - warn: schema_field_deleted_by_accept [prod] 'app.screen_view'
        Field 'user_id' was deleted from event type 'app.screen_view' on 2026-07-26T19:08:09Z when a missing_field drift was accepted (by user uid-7).
      - warn: schema_drift_open [prod]
        Project 'prod' has 1 untriaged schema drifts (oldest detected 2026-07-28T19:08:09Z): app.screen_view.cart_value (type_changed)

6 checks: 3 pass, 1 warn, 2 fail. Exit 3.
Re-run with --json for the machine-readable form of every finding.
```

Output is **ASCII only and byte-identical whether stdout is a terminal or a
pipe** — no colour, no spinners, no cursor control. `tripl doctor | tee
incident.log` and what you saw on screen are the same artifact, and the log you
paste into a ticket carries no escape codes.

The six checks always run **in this order and exactly once each**; per-project
results are *findings inside* a check, never repeated checks. `SKIP` means "I
could not look", which is never the same thing as a pass — a run where nothing
reached a verdict is reported as `fail`, not as "0 failures".

### 1. `connectivity` — instance reachability

Unauthenticated `GET /health` at the instance root (not under `/api/v1`). This
is the only probe that carries no `Authorization` header, deliberately: it
separates *"is the instance up and is its database reachable"* from *"is your
key good"*, two questions a 401 on an authenticated probe cannot tell apart.

If this check fails, **every other check is skipped** and the run ends. That is
what bounds a dead-host run to roughly one timeout instead of one per endpoint.

| Finding | What it means | What to do |
|---------|---------------|------------|
| `instance_unreachable` | DNS, TLS, connection refused or timeout. | Check the URL the header printed and where it came from — `base_url_source` names `--url`, `$TRIPL_BASE_URL` or the config file path. Then check the app container and whatever proxy fronts it: [Runbook → Health checks](./runbook.md#health-checks). |
| `instance_database_unreachable` | `/health` answered **503** `{"status":"error","component":"database"}`. The API is up; PostgreSQL is not. | This is a stack problem, not a tripl problem. See [Troubleshooting → PostgreSQL](../use/troubleshooting.md#postgresql-application-database). Almost every API request will fail until it is back. |
| `instance_not_tripl` | Something answered, but not the way a tripl instance does. | The URL points at a proxy, a load-balancer error page, or the wrong host. Compare with `curl -fsS "$TRIPL_BASE_URL/health"`. |

### 2. `auth` — API key

`GET /auth/me`. A 200 means the key is instance-wide and reports the backing
user's role; a **403 is a pass**, because a project-scoped key is refused on
every route without a `{slug}` path parameter *by design*. Only the role is
printed — never an email, never a key prefix — because doctor output gets pasted
into tickets.

| Finding | What it means | What to do |
|---------|---------------|------------|
| `api_key_invalid` | 401. The key is wrong, revoked, or expired. | The message names where the key came from. Mint a new `tk_r_` key at **Settings → API keys**; note that keys can carry an expiry. |
| `auth_unexpected_status` | Any other non-200. | Usually a proxy in front of the API rewriting the response. Inspect with `curl -i -H "Authorization: Bearer $TRIPL_API_KEY" "$TRIPL_BASE_URL/api/v1/auth/me"`. |

### 3. `projects` — project selection and provisioning

Resolves *which* projects the rest of the run judges. Without `--project`, the
instance listing is used and **demo projects are excluded** — their runtime
ticks and collection cooldown are noise in exactly the checks that matter. The
exclusion is always printed, never silent.

| Finding | What it means | What to do |
|---------|---------------|------------|
| `project_selector_required` (fail) | The key is project-scoped, so doctor cannot discover the slug. | Re-run with `--project <slug>`. See [below](#project-scoped-keys). |
| `project_list_forbidden` (fail) | `GET /projects` returned 403 for a key that is *not* project-scoped. | The backing user's role is insufficient. Use a key owned by a user with access, or name the project with `--project`. |
| `project_not_found` (fail) | A slug passed to `--project` does not exist. | Check the spelling — it is the URL slug, not the display name. |
| `project_not_authorized` (fail) | The key is fenced to a *different* project. | Use the right key, or the right slug. |
| `no_projects_selected` (warn) | The instance has no non-demo projects. | Nothing is broken. Add `--include-demo` to check the demo workspace, or `--project`. |
| `project_generation_failed` (fail) | Provisioning of a project did not finish. `generation_error` carries the reason. | The project's data is incomplete and every downstream check about it is unreliable. Re-create it, or see [Demo workspace](../use/demo-workspace.md) if it is a demo. |
| `project_generating` (warn) | Provisioning is still in progress. | Wait and re-run. Counts reported for it are partial. |

### 4. `data_sources` — warehouse connection probes

Judges only the data sources that a selected project's scan configs actually
*reference*; an unused source failing its probe is not this run's business.

:::warning A green connection probe is not evidence of health
`last_test_at` is written **only** on an explicit connection test or on a
create/update — nothing refreshes it in the background. A source that last
tested OK a month ago and has been rejecting the worker's credentials since
Tuesday still reports `success`. That is what `data_source_probe_stale` exists
to say.
:::

| Finding | What it means | What to do |
|---------|---------------|------------|
| `data_source_probe_failed` (fail) | The last connection test on a referenced source failed. | Start here — it explains the scan failures below it. `evidence.last_test_message` carries the warehouse's own error. If `message_redacted` is `true`, re-run with an **owner**-role key to see the text. Then see [Troubleshooting → A scan run fails](../use/troubleshooting.md#a-scan-run-fails--a-data-source-connection-test-fails). |
| `data_source_probe_stale` (warn) | The source last tested **OK**, but that test predates the moment its scan config started failing. | Do not trust the green. Press **Re-test connection** on that data source in the app and read the fresh result. `evidence.streak_started_at` is when the failures began. |

### 5. `scans` — scheduled metrics collection

The centre of gravity of the whole command, and the check the incident needed.
For every scan config that has a collection interval, doctor reads the newest
**200** jobs — the API's maximum — and counts the run of consecutive **failed
dispatcher jobs** at the head of the history.

That is deliberately *not* the 40-row window the dispatcher itself walks. The
scheduler only needs the backoff delay, which stops growing at six consecutive
failures, so 40 rows are ample for it. doctor answers a different question —
*how long has this been broken* — and at 40 rows a 15m config failing for four
days would report 40 failures since this morning instead of 384 since Monday.
When every job in the window is a failure the run started even earlier, so the
finding says "at least N, at least since T" and sets
`evidence.consecutive_failures_truncated`.

Dispatcher jobs are identified *positively*, by `result_summary.mode ==
"metrics_collection"`. Manual catalog scans, metrics replays, event-group
applies and demo runtime ticks share the same job table and are neither counted
as failures nor treated as the success that ends a streak — so pressing **Run
now** in the app can neither trigger the backoff nor clear it.

| Finding | What it means | What to do |
|---------|---------------|------------|
| `scan_config_not_dispatchable` (fail) | The config has an interval but **no time column**, so the scheduler's query never selects it. It is not failing; it is invisible. The app shows the same config with a **Needs a time column** badge on its row in **Govern → Scans**. | Open the scan's configuration in the app and set a time column. Nothing will ever collect until you do. |
| `scan_config_failing` (fail) | *N* consecutive scheduled runs failed. | See the walkthrough below. |
| `scan_backoff_active` (warn) | The scheduler has **deliberately** deferred the next attempt. | Nothing. This is expected behaviour — see [The retry backoff is not a hang](#the-retry-backoff-is-not-a-hang). |
| `scan_watermark_stale` (warn) | Collection is *succeeding*, but the last run only wrote data through `evidence.time_to`, more than three intervals behind now. Not raised for a demo project, whose watermark freezes while the demo is paused. | The query works and the source table is producing no rows in the scan window. Check the upstream pipeline that fills that table, and the scan's filters. This is the shape of "events frozen with no error". |
| `scan_history_window_full` (warn) | Every job in the fetched history window is a manual run, a replay or a demo tick, so whether scheduled collection runs at all could not be determined. | Not a fault by itself. Check the config's job history in the app, or trigger a scheduled collection and re-run. |
| `scan_never_collected` (fail) | The config is older than two intervals and **no** scheduled job has ever run for it. Not raised for a demo project, for the same reason as `scan_not_dispatched`. | The beat scheduler is probably not running: `docker compose logs --tail=50 celery-beat`. See [Runbook → Health checks](./runbook.md#health-checks). |
| `scan_not_dispatched` (fail) | Not failing, but nothing has been dispatched in over three intervals. Never raised for a **demo** project: the scheduler throttles demos and stops collecting a demo entirely while nobody opens it, so its silence is expected. | Same suspects as above — beat, the broker, or a worker that never picks the task up. See [Troubleshooting → RabbitMQ](../use/troubleshooting.md#rabbitmq-the-celery-broker). |
| `scan_interval_unknown` (warn) | The backend reports an interval this CLI does not know. | Upgrade the CLI. Staleness and backoff for that config could not be judged, and doctor says so rather than judging with a wrong denominator. Whether it is *failing* needs no interval, so that is still reported alongside. |

Suppression is intentional: a failing config is trivially also "not recently
dispatched", so `scan_not_dispatched` is not raised alongside
`scan_config_failing`. One root cause, one finding.

#### What to do about `scan_config_failing`

The evidence is designed to be worked top to bottom:

1. **`last_error_is_generic: true`** — the message is the backend's own
   catch-all, `"Scan failed due to an internal error."`, which is what any
   uncurated exception collapses to. **Do not quote it as the cause**; that is
   the wall the original incident hit for four days. The real traceback is in
   the worker log for the job named by `last_failed_job_id`:

   ```bash
   docker compose logs celery-worker | grep -F '<last_failed_job_id>'
   ```

   When `last_error_is_generic` is `false`, the text *is* the curated cause and
   can be read at face value.

2. **`data_source_id`** — cross-reference the `data_sources` check above. A
   `data_source_probe_failed` finding on the same id is your answer, and the
   scan failure is a symptom.

3. **`last_success_at` and `consecutive_failures`** — when it last worked, and
   how long it has been broken. If `last_success_at` is `null`, it never worked
   and the config itself is probably wrong.

4. Only after those: open the scan in the app and re-run it manually. A manual
   run neither counts toward the streak nor clears it, so it is a safe probe.

### 6. `drifts` — schema drift

One request per event type, bounded by `--max-event-types` (default 200). The
budget is spread evenly across the selected projects rather than spent in project
order, so no project is starved to zero reads by a larger one. What the budget
could not reach is **reported**, never silently omitted — and reported **per
project**, so "nothing found here" and "this project was barely looked at" never
print the same.

| Finding | What it means | What to do |
|---------|---------------|------------|
| `schema_field_deleted_by_accept` (warn) | A `missing_field` drift was **accepted**, and accepting one *deletes the field definition* from the event type. | Verify this was intended. The deletion is invisible in every other surface and is the mechanism by which a field silently vanished from a tracking plan. `resolved_at` and `resolved_by` (a user id) say when and by whom. Re-add the field if it was a mistake. |
| `schema_drift_open` (warn) | Untriaged drifts — status `open`, or `snoozed` past its snooze. | Triage them in the app. `evidence.examples` names up to three; `untriaged_count` is the real total. |
| `drift_scan_truncated` (warn) | This project has more event types than the budget reached. One finding **per affected project**; `evidence.examined` and `evidence.total` are that project's own counts, and a project examined in full raises no finding at all. | Raise `--max-event-types`, or narrow the run with `--project`. Until then, treat the named project's drift result as partial. |

### `endpoint_unexpected_status`

This code can appear in `projects`, `scans` and `drifts`, and it
always means the same thing: **a read did not return 200, so the state it
reports is unknown — not empty.** doctor never treats a non-200 as an empty
list. A 404 read as "no drifts" is exactly the class of mistake this command
exists to eliminate. `evidence.path` and `evidence.status_code` name the call.

The `data_sources` check is the one exception, and deliberately: a non-200
there becomes a **`skip`** with a `skip_reason`, because a read key that
cannot list data sources is a normal, unfixable fact about that key rather
than an instance fault. A skip never counts as a pass, so a run in which
every check skipped still exits non-zero.

### Project-scoped keys

A key bound to a single project is refused on every instance-level route by
design. doctor detects that *positively and first*, off `/auth/me`, so it can
tell you the right thing instead of inferring it from some later 403:

```bash
tripl doctor --project prod
```

```text
tripl doctor - https://tripl.example.com (from $TRIPL_BASE_URL)

PASS  connectivity  Reached https://tripl.example.com (from $TRIPL_BASE_URL); the API and its database are up.
PASS  auth          The API key authenticates and is scoped to a single project, so instance-wide endpoints are refused by design.
PASS  projects      1 project selected.
SKIP  data_sources  GET /data-sources is instance-level and returns 403 for a project-scoped key by design; re-run with an instance-scoped key to see data-source probe results
PASS  scans         1 scheduled scan configs are collecting on time.
PASS  drifts        1 event type(s) examined; no untriaged schema drift.

6 checks: 5 pass, 1 skip. Exit 0.
```

`--strict` deliberately does **not** promote skips: a project-scoped key would
otherwise fail every strict run forever, for a reason the operator cannot fix.

`tripl status`, `tripl watch`, `tripl scans list` and `tripl drifts list` also
need `--project` with such a key. None of them has a verdict contract, so they
simply fail with the API's own 403 message — extended with the hint *"If this
key is scoped to a single project, that 403 is expected"* and the flag to type.
Pass `--project`.

The commands that act on **one** object — `scans jobs`, `scans run`,
`scans cancel`, `drifts dismiss`, `drifts reopen` — require `--project` exactly once from
everybody, project-scoped key or not, so they never reach the instance listing
and never produce that 403.

### Request cost

A healthy one-project instance costs **8 requests**: `/health`, `/auth/me`,
`/projects`, `/data-sources`, then per project `/scans` and `/event-types`, then
one `/jobs` call per dispatchable scan config and one `/drifts` call per event
type. At most **6 requests are in flight at once** — a diagnostic that saturates
the instance it is diagnosing reports on the load it created.

## The retry backoff is not a hang

This deserves its own section because mistaking it for a stuck worker cost a
real investigation.

After **3** consecutive failed scheduled runs, the dispatcher stops retrying at
the config's normal cadence and starts backing off exponentially: the delay is
`interval × 2^(streak − 3)`, floored at one interval and capped by the smaller
of **8 intervals** and **24 hours**. From the outside this looks exactly like a
worker that has stopped — no new jobs appear for hours — which is why doctor
reports it as a distinct, `warn`-level finding that says so in words:

```text
      - warn: scan_backoff_active [prod] 'prod events'
        The scheduler has deliberately deferred the next attempt to not before 2026-07-31T22:08:09Z (about 4h after the last failure): 3 or more consecutive failures trigger a backoff, so the worker is not stuck.
```

How long the deferral is, by interval and streak length:

| Consecutive failures | 15m | 1h | 6h | 1d | 1w |
|---|---|---|---|---|---|
| 1–2 | *no wait* | *no wait* | *no wait* | *no wait* | *no wait* |
| 3 | 15m | 1h | 6h | 24h | 7d |
| 4 | 30m | 2h | 12h | 24h | 7d |
| 5 | 1h | 4h | 24h | 24h | 7d |
| 6 and beyond | 2h | 8h | 24h | 24h | 7d |

A `1d` or `1w` config therefore never backs off past its own cadence — the floor
and the ceiling meet.

:::note These numbers are an estimate, and the field says so
The CLI carries its own copy of the scheduler's constants, so the JSON key is
`deferred_by_seconds_estimate` and the printed sentence says "about". The rule
that produced the number ships alongside it as `evidence.backoff_after`, so a
reader can always see the arithmetic. Do not build an alert threshold on the
exact value.
:::

**What to do:** nothing about the backoff itself. Fix the cause named by
`scan_config_failing`; the first successful run clears the streak and the
config returns to its normal cadence immediately.

## `tripl status`

```
usage: tripl status [-h] [--url URL] [--api-key KEY] [--config PATH]
                    [--project SLUG] [--include-demo] [--json] [--days N]
                    [--timeout SECONDS]
```

A snapshot, not a verdict. **It always exits 0 when it completed**, however bad
the numbers are — a daily digest that returns non-zero is a cron job somebody
disables. Use `tripl doctor` when you want an exit contract.

```text
tripl status - https://tripl.example.com (from $TRIPL_BASE_URL)

prod (Prod)
  events     412 total, 388 active, 17 event types
  scans      4 configured, 0 failing
  signals    0 significant open
  monitors   0 firing
  coverage   91.4% over 7 days (388/425 matched)

mobile (Mobile)
  events     98 total, 95 active, 6 event types
  scans      2 configured, 1 failing
  signals    3 significant open
  monitors   1 firing
  coverage   91.4% over 7 days (388/425 matched)
```

`--days N` (default 7, max 180) sets the reconciliation coverage window. Demo
projects are excluded unless `--include-demo` is given, exactly as in doctor.

:::tip These numbers are the app's numbers
Every count comes from the project summary the **backend** computes, not from a
second calculation in the CLI. `signals` is the same significant-open-signal
count as the app's badge and `monitors` the same firing count — they agree by
construction, not by coincidence. This matters: the "significant" threshold is
already defined in two places in this repository, and a third copy in the CLI
would be a drift waiting to happen.
:::

Cost is one request for the project listing plus one coverage request per
project — **2 requests** for a single-project instance, 3 for the two shown
above. With `--project`, the listing is replaced by one read per named slug.

:::note `status` and `doctor` count failing scans differently
`status` prints the server-side rollup `failing_scan_config_count`; `doctor`
derives its own count by walking each config's job history. They normally agree.
If they ever disagree in the field, `doctor`'s number is the one with the
evidence attached — read the `scans` findings.
:::

## `tripl watch`

```
usage: tripl watch [-h] [--url URL] [--api-key KEY] [--config PATH]
                   [--project SLUG] [--include-demo] [--scan NAME_OR_ID]
                   [--interval SECONDS] [--duration SECONDS]
                   [--stall-after SECONDS] [--json] [--timeout SECONDS]
```

`doctor` answers **"what is broken"** at one instant and then exits. `watch`
answers **"what is happening right now"**: it stays attached and prints one line
every time something moves — a replay advancing from chunk 4 to chunk 5, a job
finishing, a signal opening, an alert delivery failing to page anyone. It is the
command for the half hour *after* the page, when the question is no longer "is
something wrong" but *"is this scan hung, or just slow?"* — the exact question an
operator answered by hand over ssh for four days during the 2026-07-28..31
incident.

It reaches **no verdict**. Whatever it observes, a completed run exits 0 and it
never exits 3. A green `watch` run proves strictly *less* than a green `doctor`
run — watch only ever saw what fell inside its polls — so building a CI gate on
it would be a slower, flakier `doctor --strict`. Use `doctor` when you want an
exit contract.

:::note It polls. There is no daemon and no subscription.
tripl does have a Server-Sent Events stream, and `watch` deliberately does not
use it. The replay chunk counter — this command's headline — is written straight
into the scan job's `result_summary` by the worker's progress heartbeat, with no
realtime event published alongside it, so it is **invisible on that bus**. The
bus also degrades to silence when Redis is absent while the socket still looks
healthy. Polling the REST API is more transport for strictly more information.
:::

| Flag | Meaning |
|------|---------|
| `--project SLUG` | Follow only this project. Repeatable. Required for a project-scoped key. |
| `--include-demo` | Also follow demo projects, which are excluded by default. |
| `--scan NAME_OR_ID` | Follow only these scan configs, matched **exactly** on name and then on id. Repeatable. Narrows the **job** lines only. |
| `--interval SECONDS` | Seconds between polls, default `10`, range 2–3600. |
| `--duration SECONDS` | Stop after this many seconds and exit 0, range 1–86400. Default: run until `Ctrl-C`. |
| `--stall-after SECONDS` | Report a running job whose progress has not moved for this long, default `120`, range 10–86400. |
| `--json` | JSON Lines on stdout, one object per event; every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

`--scan` never filters signals or deliveries. A `metric`-scope signal carries no
scan config at all, so filtering the whole feed by scan config would make exactly
the anomalies an incident is built from disappear.

A session: a replay is already running when you attach, it advances, a signal
opens, an alert delivery fails, the replay finishes, and you press `Ctrl-C`.

```text
tripl watch - https://tripl.example.com (from $TRIPL_BASE_URL)

Following 1 project, 1 scan config. Polling every 10s; signals,
deliveries and the scan listing at most every 30s. 1 request per tick,
4 per slow tick. Ctrl-C to stop.

prod (Prod)
  running    'nightly replay' job job-91c2 (metrics_replay) chunk 3 of 18 (16.7%) collecting, started 2m ago
  pending    none
  signals    0 open across all scopes (baseline); the project summary counts 0 as significant
  deliveries 0 failed in the newest 20 (baseline)

2026-07-31T19:10:41Z  watch.started    1 project, 1 scan config, poll 10s.
2026-07-31T19:10:51Z  job.progress     [prod] 'nightly replay' job job-91c2 chunk 4 of 18 (22.2%) collecting 2026-07-05T00:00:00Z..2026-07-06T00:00:00Z, 2m elapsed.
2026-07-31T19:11:01Z  job.progress     [prod] 'nightly replay' job job-91c2 chunk 5 of 18 (27.8%) collecting 2026-07-06T00:00:00Z..2026-07-07T00:00:00Z, 2m elapsed.
2026-07-31T19:11:11Z  job.progress     [prod] 'nightly replay' job job-91c2 chunk 6 of 18 (33.3%) collecting 2026-07-07T00:00:00Z..2026-07-08T00:00:00Z, 2m elapsed.
2026-07-31T19:11:11Z  signal.opened    [prod] 'nightly replay' project_total drop at 2026-07-31T19:00:00Z: actual 412 vs expected 1180 (z=-6.1).
2026-07-31T19:11:11Z  delivery.failed  [prod] 'Checkout drop' -> slack 'oncall' failed: 'channel_not_found'. Nobody was paged; delivery del-4f21.
2026-07-31T19:11:21Z  job.finished     [prod] 'nightly replay' job job-91c2 completed after 3m (18 of 18 chunks).
2026-07-31T19:11:21Z  watch.stopped    stopped (interrupted) after 40s, 5 ticks, 12 requests.
```

The layout is fixed: RFC 3339 timestamp, event token, `[project]`, message, with
the message always starting at column 39. Like `doctor`, output is **ASCII only
and byte-identical whether stdout is a terminal or a pipe** — no colour, no
spinner, no redraw, no cursor control. That is not decoration for a follow mode;
it is the point. `tripl watch | tee incident.log` has to produce the artifact you
actually saw, and a live-updating dashboard would produce neither. The preamble
holds one more invariant worth knowing: **no preamble line starts with a
timestamp and every stream line does**, so

```bash
tripl watch | grep -E '^[0-9]{4}-'
```

is the event stream on its own.

### The first screen

A follow mode that only reported transitions it personally observed would tell
an operator who started it thirty seconds late that nothing is happening. So the
first successful read of every stream **seeds** the state — and then the preamble
prints what that seed found, before a single event line:

- the **cadence and the request budget**, so the load this command adds to an
  already-unwell instance is stated rather than guessed at;
- every **running** and **pending** job, with its mode and, for a replay, its
  current chunk, percentage, phase and age. This is the "is it hung?" answer at
  attach time;
- the **open signal count across all scopes**, marked `(baseline)`, printed
  beside the project summary's own significance-filtered count. Two different
  populations, both labelled — never one silently standing in for the other;
- the count of **failed deliveries in the newest 20**, also `(baseline)`.

Signals are **counted, not listed**. A 200-signal incident would bury the stream
before it began; `tripl status` is the command for the list.

Everything in the preamble is therefore *already known* and is never re-emitted
as an event. Subsequent silence means "no new ones", not "nothing wrong".

### What counts as new

Each tick builds a fresh snapshot and diffs it against the previous one. There is
no growing "everything I have printed" set, so memory is flat over an eight-hour
run.

| Stream | Identity | Reported when |
|--------|----------|---------------|
| Jobs | The job id, within the newest **10** jobs of each followed scan config | `status`, `replay_progress_phase`, `replay_chunks_completed` or `replay_current_chunk_index` changed |
| Signals | `(scan_config_id, scope_type, scope_ref)` — the backend's own key | The identity is new, or its `bucket`, `direction` or `state` changed |
| Deliveries | The delivery id, within the newest 20 **failed** deliveries | The row is `failed` and was not already reported as failed |

Three consequences are worth stating outright:

- **`updated_at` is deliberately not part of a job's identity of change.** The
  worker's progress heartbeat bumps it even on the ticks where no counter moves.
  Including it would print a line every heartbeat and, far worse, would make
  `job.stalled` unreachable — a heartbeating-but-wedged job would look alive,
  which is precisely backwards. `updated_at` still ships in the JSON `data`.
- **A signal's bucket is not part of its identity.** If it were, a signal
  advancing to a later bucket would produce a `signal.cleared` followed by a
  `signal.opened` — a fabricated resolution in the middle of a live incident.
- **A job disappearing is not an event.** The job list is a window ordered newest
  first, so a row leaving it is an artifact of the window, not a thing that
  happened. A *signal* disappearing **is** reported, because the signals endpoint
  returns the complete active set with no window.

A scan config created mid-run is discovered on the slow clock and then seeds
silently, exactly like the ones present at startup. A **project** created mid-run
is not picked up — restart `watch` for that.

### Event tokens

`event` is a stability contract; the message beside it is prose and may be
reworded in any release.

| Token | Stream | Emitted when |
|-------|--------|--------------|
| `watch.started` | `meta` | Once, right after the first screen. Carries the resolved options and the baseline counts. |
| `watch.stopped` | `meta` | Once, last. `data.reason` is `interrupted`, `duration_elapsed` or `authentication_failed`. |
| `job.queued` | `event` | A job is `pending` — newly seen, or moved back into it. |
| `job.started` | `event` | A job is `running`. Also fires for a job first seen already running. |
| `job.progress` | `event` | A **running `metrics_replay`** job's chunk counter or phase moved. The line the incident needed. |
| `job.stalled` | `event` | A **`metrics_replay`** job's published progress has not moved for `--stall-after` seconds. |
| `job.unchanged` | `event` | A job with **no progress channel** has not changed status for `--stall-after` seconds — a `pending` job no worker has picked up, or a long-running `metrics_collection`. |
| `job.finished` | `event` | `completed`. For a replay, the message carries the final chunk count. |
| `job.failed` | `event` | `failed`. Carries `error_message`, or says there was none. |
| `job.cancelled` | `event` | `cancelled`. |
| `signal.opened` | `event` | A signal identity that was not in the previous active set. |
| `signal.updated` | `event` | An already-open signal whose bucket, direction or state moved. |
| `signal.cleared` | `event` | A signal left the active set. |
| `delivery.failed` | `event` | An alert delivery is in status `failed`. Nobody was paged. |
| `poll.degraded` | `diagnostic` | A read did not return 200, or a full window may have hidden rows. |
| `poll.recovered` | `diagnostic` | A stream that had been failing answered again. |

A scheduled collection job — `result_summary.mode == "metrics_collection"` — has
no chunk counters, so it produces `job.queued` / `job.started` / `job.finished` /
`job.failed` and never `job.progress`. That is not a gap in `watch`: the backend
publishes no progress for those jobs, and `watch` does not invent progress it was
not given. `job.progress` is a **`metrics_replay` line**.

:::tip `signal.cleared` says "cleared", not "resolved"
A signal leaving the active set may only mean its freshness window closed. The
line claims exactly what was observed and nothing more, and it prints how many
signals are still open in that project so a disappearance never reads as an
all-clear.
:::

#### `job.stalled` is a statement about observation

It says *watch has seen no progress since T* — never *this job is broken*. A
replay chunk over a large window legitimately takes minutes.

Which is also why there are **two** tokens rather than one. Only a
`metrics_replay` job publishes a progress counter; a scheduled
`metrics_collection` job and a job still sitting in `pending` publish nothing
that *could* move short of their status. Reporting those as `job.stalled` would
claim an observation watch was never in a position to make — and, because such a
job's state cannot change while it runs, it would fire on *every* long
collection, which is a false alarm with a schedule. So they get
`job.unchanged`, whose message says what was actually seen: *still in status
`pending` after 4m of watching (no worker has picked it up)*. That sentence is
the one you want when the queue is not draining, which is the most common way a
scan appears to hang.

Both tokens carry the same `unchanged_since` and `unchanged_seconds` evidence
and share the re-report schedule described next.

The threshold is counted in **seconds, not polls**, so `--interval 60` does not
silently turn a 120-second threshold into a two-hour one. A condition that
persists re-reports on a **power-of-two schedule** — at 1x, 2x, 4x, 8x the
threshold — which is roughly a dozen lines over four hours instead of one every
tick, while still telling you it is still going. Any observed progress restarts
the clock, so there is no "unstalled" line to keep track of.

The schedule is easiest to see at a threshold far below the default, which is
what produced the three lines below — 30s, then 60s, then 2m, at 1x, 2x and 4x:

```bash
tripl watch --stall-after 30
```

```text
2026-07-31T19:11:11Z  job.stalled      [prod] 'nightly replay' job job-91c2 unchanged for 30s at chunk 5 of 18 (27.8%) collecting; watch has seen no progress since 2026-07-31T19:10:41Z.
2026-07-31T19:11:41Z  job.stalled      [prod] 'nightly replay' job job-91c2 unchanged for 60s at chunk 5 of 18 (27.8%) collecting; watch has seen no progress since 2026-07-31T19:10:41Z.
2026-07-31T19:12:41Z  job.stalled      [prod] 'nightly replay' job job-91c2 unchanged for 2m at chunk 5 of 18 (27.8%) collecting; watch has seen no progress since 2026-07-31T19:10:41Z.
```

### When a poll fails

**The run continues.** A follow tool that exits when the thing it follows goes
down is exactly backwards — an operator watching an instance through a rolling
restart wants `watch` still there when it comes back, and exiting would truncate
a `| tee incident.log` capture at the worst possible moment.

A failed read **does not update its stream's snapshot**. Diffing against an empty
list would print `signal.cleared` for every open signal and then reprint them all
as `signal.opened` on recovery — lying twice, during the incident. Holding the
last good snapshot turns an outage into a reporting **delay**: the next good poll
fires every transition that happened while `watch` was blind, late but complete.

```text
2026-07-31T19:11:11Z  poll.degraded    [prod] signals read failed: HTTP 500 on /projects/{slug}/anomalies/signals. Signal lines are suspended until it recovers - no signal lines does NOT mean no signals.
2026-07-31T19:11:21Z  poll.degraded    [prod] signals read failed: HTTP 500 on /projects/{slug}/anomalies/signals. Signal lines are suspended until it recovers - no signal lines does NOT mean no signals.
2026-07-31T19:11:41Z  poll.recovered   [prod] signals read recovered after 30s (3 failed polls); 1 events reported from the gap.
```

Note the arithmetic: three polls failed, two lines were printed. `poll.degraded`
follows the same power-of-two schedule as `job.stalled` — the 1st, 2nd, 4th, 8th
consecutive failure of a stream gets a line and the rest are quiet.
`poll.recovered` is never throttled, and it carries the number that matters:
**how many events came out of the gap**, which is how you learn that a quiet
stretch was `watch` being blind rather than the instance being calm.

Two more shapes of degradation:

- **A 403 is only ever a line.** It is legitimately per-project and per-role, and
  the other projects are still reporting usefully. If every project 403s you get
  a wall of lines naming the 403, and you can stop the run.
- **A full window in which every row is new** also raises `poll.degraded`, with
  `window_full: true`. It means older rows may have been pushed out between
  polls; lower `--interval` or narrow the run. Merely hitting the limit is *not*
  reported — a healthy config with ten jobs of history does that every poll and
  it means nothing.

**A 401 is the one failure that ends the run.** The key is gone and waiting
cannot fix it, and continuing would hammer an auth path that is logged and rate
limited. `watch` prints the `poll.degraded` line, then `watch.stopped` with
`reason: "authentication_failed"`, then the usual `tripl: ...` error, and exits
1.

The run can also be **refused before it prints anything**: `watch` needs the scan
listing to know what to follow, so if `GET /projects/{slug}/scans` fails during
startup the command fails there rather than half way through a feed.

:::warning A degraded slow stream is retried at the fast interval
Signals, deliveries and the scan listing are normally polled at most every 30
seconds. That clock only advances on a **successful** read, so while one of them
is failing it is retried on every tick — three times the usual rate against an
instance that is, by assumption, already unwell. Raise `--interval` if you are
watching an instance through a sustained outage.
:::

### Request cost

Per tick: **one request per followed scan config**, asking for the newest
**10** jobs. That window is deliberately far smaller than `doctor`'s 200 —
`doctor` asks *how long has this been broken* and needs history, `watch` asks
*what appeared since the last poll* and needs recency, and 20x the payload on a
loop that repeats every ten seconds is a load problem this repository has already
paid for once.

On a **slow tick** — at most once every 30 seconds per project — three more
requests are added per project: the scan listing, the open signals
(`expanded=true`, so event-scope anomalies are not dropped), and the newest 20
**failed** alert deliveries. The 30 seconds is not invented: the backend caches
the unfiltered active-signals query for exactly that long, so polling faster is
provably wasted work.

At most **6 requests are in flight at once**, and only one tick is ever in
flight: the interval is measured *after* the previous tick finished, never on a
fixed wall clock and never with catch-up. An instance that answers slowly is
therefore automatically polled less, and a 40-second stall can never be followed
by a burst of queued ticks.

`watch` **refuses to start** above **24** selected scan configs rather than
truncating, and says so with the flags to narrow it. A one-shot command can
report what its budget could not reach; a command that repeats would have to
reprint that warning every tick, or print it once at the top where it scrolls
away and leaves you reading a feed silently missing the config the incident is
in.

:::note A config deleted mid-run keeps being polled
It produces `poll.degraded` lines with a 404, on the power-of-two schedule, until
you restart. This is deliberate: silently dropping a target is the exact failure
mode `watch` exists to avoid.
:::

## Write safety

`doctor`, `status` and `watch` only ever read. **Five verbs do not**, and this
is the one table to read before you run any of them:

| Command | What it changes on the instance | Key | Backing role | Asks first |
|---------|---------------------------------|-----|--------------|------------|
| `tripl scans run` | Queues a scan job, which executes the config's stored SQL against your warehouse. | `tk_w_` | editor or owner | **No** |
| `tripl scans cancel` | Stops a `pending` or `running` job. A running job is not killed: it stops at its next checkpoint — a metrics run at the next chunk boundary, keeping the metrics it already wrote; a catalog run or an event-group apply immediately before its write commits, so stopped there it writes nothing at all. Stopped after that commit, the work it had already made durable stays. Either way a stopped job stays `cancelled`. | `tk_w_` | editor or owner | **Yes** |
| `tripl drifts dismiss` | Moves one schema drift to `false_positive` or `snoozed`, which takes it out of `doctor`'s untriaged count. | `tk_w_` | editor or owner | **Yes** |
| `tripl drifts reopen` | Moves one schema drift back to `open`, and **discards** its resolution note and resolver. | `tk_w_` | editor or owner | **Yes** |
| `tripl annotate` | Adds one chart annotation with source `api`. The same label posted again with source `api` within 24 hours is **de-duplicated**: nothing new is created. | `tk_w_` | editor or owner | **No** |

Everything else on this page — `doctor`, `status`, `watch`, `scans list`,
`scans jobs`, `drifts list` — is `GET`-only and needs nothing but `tk_r_`.

### The CLI does not judge your key

There is **no local `tk_r_` / `tk_w_` check**. The prefix is derived server-side
from the scope's first letter and says nothing at all about the backing user's
role, so a client-side gate would be a fourth copy of a backend rule *and* still
wrong for a `tk_w_` key held by a viewer. The request goes out and the API's own
403 is printed, naming all three possibilities at once:

```text
tripl: Forbidden (403): the API key lacks the required scope (tk_r_ keys cannot write), is scoped to a different project, or the backing user role is insufficient. API detail: API key has read-only scope
```

Read `API detail:` — it is the server's own sentence and it says which of the
three actually applied.

### `--dry-run` is on all five

It resolves everything a real invocation would resolve — including turning a
`<scan>` name into a config id, which is where a typo becomes exit 2 — prints
the exact request, and **sends nothing**:

```bash
tripl scans run 'prod events' --project prod --dry-run
```

```text
tripl scans run - https://tripl.example.com (from $TRIPL_BASE_URL)

dry run: would send POST /projects/prod/scans/scan-1/run with body None
Nothing was sent.
```

The printed request is **method, path, params and body, and nothing else** — no
headers, no `Authorization`, no API key. A credential that can be printed
eventually is, so it is excluded at the one place the request is projected for
printing. `--dry-run` also short-circuits the confirmation prompt, because there
is nothing to confirm.

:::warning The dry-run body is Python's spelling, not JSON
`with body {'action': 'false_positive'}` and `with body None` are the *repr* of
the body, so quotes are single and null is `None`. Do not paste that into
`curl`. The `--json` document carries the same body as real JSON — use
`tripl drifts dismiss ... --dry-run --json | jq .request` if you want something
a machine can consume.
:::

### The confirmation rule

`scans cancel`, `drifts dismiss` and `drifts reopen` prompt. `scans run` and
`annotate` do not, and neither has a `--yes` at all — passing one is exit 2,
because a flag that does nothing here is a flag a script author will assume does
something on the next command too. The reasoning is that `run` executes SQL an
owner already authored, on a schedule that already runs it, and `annotate` adds
a marker that is cheap to delete from a pipeline that has nobody to ask;
`cancel` throws away work in flight, `dismiss` hides a finding from `doctor`,
and `reopen` destroys the note that says why the finding was hidden.

```bash
tripl scans cancel 'prod events' job-91c2 --project prod
```

```text
Cancel job job-91c2 of prod 'prod events'? It stops at the next chunk boundary; metrics already written are kept. [y/N] y
tripl scans cancel - https://tripl.example.com (from $TRIPL_BASE_URL)

prod 'prod events' (scan-1): job job-91c2 is now cancelled.
```

The question goes to **stderr**, not stdout — `input()` would have put prose
inside the one document `--json` promises. Anything but `y` or `yes` — including
an empty line and end-of-file — aborts with `tripl: aborted. Nothing was sent.`
and **exit 1**, never 0: a script must never be able to read "the operator said
no" as "the mutation happened".

:::warning In a pipeline, `--yes` is mandatory, not optional
When stdin is not a terminal and `--yes` was not given, `scans cancel`,
`drifts dismiss` and `drifts reopen` **refuse**: they print the question, name
`--yes`, exit **2** and send nothing.

```text
tripl: Mark drift drift-1 of prod as false_positive? It stops appearing in `tripl doctor`'s untriaged count. Refusing to prompt because stdin is not a terminal. Re-run with --yes to confirm non-interactively. Nothing was sent.
```

Both halves of that rule are deliberate. Prompting would hang the cron job that
invoked it, forever; proceeding silently would make a piped invocation more
dangerous than a typed one, and would make `--yes` meaningless.
:::

### A `201` is not proof the scan started

`scans run` reads the job it gets back. The backend catches a broker failure
inside the trigger and still answers `201`, with the job already in status
`failed` and an `error_message` on it — so a script that checked only the status
code would report a scan that never started as started. The CLI prints the
message and exits **1**:

```text
tripl: the job was created but is already failed: broker unavailable
```

### What is deliberately not here

Two operations exist in the REST API and will not be added to this CLI.

- **A bounded metrics replay.** `POST /projects/{slug}/scans/{scan_id}/metrics/replay`
  is guarded by `get_owner_user`, which rejects **any** request carrying an API
  key scope — read or write, editor or owner. It is owner-*session*-only, so a
  Bearer-token client cannot reach it at all and a `tripl scans replay` would
  `403` every time. `scans cancel` ships in its place. Whether an owner-role
  `tk_w_` key should be let through is an open backend question.
- **Accepting a schema drift.** On a `missing_field` drift, accepting *deletes
  the field definition* from the event type — the exact damage doctor's
  `schema_field_deleted_by_accept` finding exists to report. The tool that
  reports that damage must not be the easiest way to cause it, so accepting
  stays in the tripl app. `dismiss` sends `false_positive` or `snooze`,
  [`reopen`](#tripl-drifts-reopen) sends `reopen`, and no flag on either reaches
  `accept`.

  The API refuses one slice of that damage on its own: accepting a
  `missing_field` drift for a column a scan config's **event name format** builds
  event names from answers `409`, because the delete would fail every subsequent
  collection. It carries a `force` override for the case where that guard
  over-fires, and `force` has no flag here either — it is reachable only from a
  request body you write yourself, which is the point. See
  [Schema drift](../use/feature-reference.md#schema-drift).

## `tripl scans`

`doctor` tells you a scan config is failing and `watch` follows one while it
runs. `tripl scans` is what you type **in between**: which configs exist and
would the scheduler ever pick them up, what has this one been doing, start it
now, stop the one that is stuck.

```
usage: tripl scans [-h] [--url URL] [--api-key KEY] [--config PATH] <verb> ...
```

Four verbs: `list`, `jobs`, `run`, `cancel`. A bare `tripl scans` with no verb
prints this group's help **on stderr** and exits **2** — the same rule a bare
`tripl` follows, for the same reason: a script that invoked a group with no verb
has a bug, and exiting 0 would let it look successful.

:::note Why `scans list` and not `list-scans`
A command acting on the instance as a whole is one word (`doctor`, `status`,
`watch`); a command acting on a class of objects is `<plural-noun> <verb>`. The
flat spelling was rejected because it adds a top-level entry per verb and has no
answer for the sixth.
:::

### `tripl scans list`

```
usage: tripl scans list [-h] [--url URL] [--api-key KEY] [--config PATH]
                        [--project SLUG] [--include-demo] [--json]
                        [--timeout SECONDS]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | List only this project. Repeatable. Required for a project-scoped key. |
| `--include-demo` | Also list demo projects, which are excluded by default. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

```text
tripl scans list - https://tripl.example.com (from $TRIPL_BASE_URL)

prod (Prod)
  scan-1  prod events       1h   event_time  scheduled
  scan-2  checkout funnel   15m  event_time  scheduled
  scan-3  ad-hoc backfill   -    event_time  not scheduled (no interval)
  scan-4  legacy pageviews  1d   -           not scheduled (no time column)

4 scan configs in 1 project.
```

The columns are id, name, collection interval, time column, and **why the
dispatcher would or would not select this config**. That last one is the reason
the command is worth having: the scheduler's query needs an interval *and* a
time column, both set, so `not scheduled (no time column)` names a config that
is not failing — it is invisible, and nothing will ever collect for it. That is
the same condition doctor reports as `scan_config_not_dispatchable`, evaluated
by the predicate doctor itself uses to decide whose job history is even worth
reading — and the same one the web UI badges as **Needs a time column**.

`base_query` and roughly twenty tuning knobs are **omitted**. Free-text SQL runs
to kilobytes and answers no operational question; read the full configuration in
the app.

:::warning A project whose listing failed still counts in the footer
A failed read is a printed `unavailable` line, an `errors[]` entry in the JSON,
and **exit 1** — never a shorter table at exit 0.

```text
mobile (Mobile)
  /projects/mobile/scans: unavailable (Forbidden (403): the API key lacks the required scope (tk_r_ keys cannot write), is scoped to a different project, or the backing user role is insufficient. API detail: Not authorized for project)

4 scan configs in 2 projects.
1 read failed; the list above is incomplete.
```

Note what the footer says: *4 configs in 2 projects*, because the project that
403'd is still one of the two selected. The count is of what arrived, the
denominator is of what was asked for, and the line underneath is the one that
tells you they are not the same number — and it **counts** the failures, so one
403 out of six projects reads differently from six. It is the same sentence
`drifts list` prints for the same condition, deliberately: one grep over an
incident log finds both.
:::

**Cost:** one request for the project listing (or one per named `--project`
slug), plus one per project.

### `tripl scans jobs`

```
usage: tripl scans jobs [-h] [--url URL] [--api-key KEY] [--config PATH]
                        [--project SLUG] [--limit N] [--json]
                        [--timeout SECONDS]
                        <scan>
```

| Flag | Meaning |
|------|---------|
| `<scan>` | Scan config **name or id**, matched exactly — name first, then id. |
| `--project SLUG` | **Required**, exactly once. |
| `--limit N` | How many jobs to ask for, `1`–`200`, default `50`. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

This is the command that hands you a job id for `tripl scans cancel`.

```bash
tripl scans jobs 'prod events' --project prod --limit 3
```

```text
tripl scans jobs - https://tripl.example.com (from $TRIPL_BASE_URL)

prod 'prod events' (scan-1), newest 3 jobs requested:
  job-91c2  running    created 2026-07-31T19:08:41Z  finished -
  job-70ab  failed     created 2026-07-31T18:08:41Z  finished 2026-07-31T18:09:02Z  Scan failed due to an internal error.
  job-4d19  completed  created 2026-07-31T17:08:41Z  finished 2026-07-31T17:08:58Z

3 jobs.
```

The header says *requested*, not *returned*: the footer counts what came back,
and the two differ whenever the config has less history than you asked for.

The default of **50** is the API's own default, deliberately not doctor's 200.
`scans jobs` answers *what has this config been doing lately*, which a screenful
covers; doctor asks *how long has this been broken* and needs the maximum. It is
one flag away — `--limit 200`.

**Cost:** two requests — the project's scan listing, to resolve `<scan>`, then
the job history.

:::note `<scan>` is matched exactly, and refuses to guess
Exact on the name first, then on the id. Never a substring, never
case-insensitive: a `scans run` that triggered the wrong SQL because two names
share a prefix is worse than one that refuses to start. A selector matching
nothing exits 2 and lists the candidates; a name shared by two configs exits 2
and names both ids so you can pass one. It is literally `watch`'s `--scan`
matcher, so `tripl scans run 'prod events'` and
`tripl watch --scan 'prod events'` cannot mean different configs.
:::

### `tripl scans run`

```
usage: tripl scans run [-h] [--url URL] [--api-key KEY] [--config PATH]
                       [--project SLUG] [--dry-run] [--json]
                       [--timeout SECONDS]
                       <scan>
```

| Flag | Meaning |
|------|---------|
| `<scan>` | Scan config name or id, matched exactly. |
| `--project SLUG` | **Required**, exactly once. |
| `--dry-run` | Resolve everything, print the request, send nothing. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

**Write. Needs a `tk_w_` key backed by an editor or owner. Does not prompt, and
has no `--yes`.**

```bash
tripl scans run 'prod events' --project prod
```

```text
tripl scans run - https://tripl.example.com (from $TRIPL_BASE_URL)

prod 'prod events' (scan-1): started job job-91c2 (pending).
Follow it with: tripl watch --project prod --scan 'prod events'
```

The second line is the point of the pairing: `run` starts the job and returns
immediately, and `watch` is the command that tells you whether it is progressing
or wedged.

This is the same route the app's **Run now** button posts to, so the rule
described under [`scans`](#5-scans--scheduled-metrics-collection) applies
unchanged: doctor identifies dispatcher jobs positively, so a manual run neither
counts toward the consecutive-failure streak nor clears it. It is a safe probe
while you are debugging a failing config.

**Cost:** two requests, or one with `--dry-run`.

### `tripl scans cancel`

```
usage: tripl scans cancel [-h] [--url URL] [--api-key KEY] [--config PATH]
                          [--project SLUG] [--dry-run] [--yes] [--json]
                          [--timeout SECONDS]
                          <scan> <job-id>
```

| Flag | Meaning |
|------|---------|
| `<scan>` `<job-id>` | The config (name or id) and the job to cancel. |
| `--project SLUG` | **Required**, exactly once. |
| `--dry-run` | Resolve everything, print the request, send nothing. Never prompts. |
| `--yes` | Skip the confirmation. **Required when stdin is not a terminal.** |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

**Write. Needs a `tk_w_` key backed by an editor or owner. Prompts.** See
[the confirmation rule](#the-confirmation-rule).

```text
tripl scans cancel - https://tripl.example.com (from $TRIPL_BASE_URL)

prod 'prod events' (scan-1): job job-91c2 is now cancelled.
```

A job that is neither `pending` nor `running` answers **409**, which surfaces as
exit 1 with the API's own sentence:

```text
tripl: tripl API rejected the request (409): Scan job is not active (status: completed)
```

**Cost:** two requests, or one with `--dry-run`.

## `tripl drifts`

Schema drift is the gap between the tracking plan and what the warehouse
actually carries. doctor reports it as a count and up to three examples;
`tripl drifts list` is the full list, and `tripl drifts dismiss` is how you take
one row out of the untriaged pile without opening the app.

```
usage: tripl drifts [-h] [--url URL] [--api-key KEY] [--config PATH] <verb> ...
```

Three verbs: `list`, `dismiss` and `reopen`. A bare `tripl drifts` prints this
group's help on stderr and exits 2.

### `tripl drifts list`

```
usage: tripl drifts list [-h] [--url URL] [--api-key KEY] [--config PATH]
                         [--project SLUG] [--include-demo] [--status STATUS]
                         [--max-event-types N] [--json] [--timeout SECONDS]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | List only this project. Repeatable. Required for a project-scoped key. |
| `--include-demo` | Also list demo projects, which are excluded by default. |
| `--status STATUS` | `open`, `accepted`, `snoozed`, `false_positive`, `untriaged` or `all`. Default `untriaged`. |
| `--max-event-types N` | Read budget for the fan-out, default `200`, range 1–10000. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

```text
tripl drifts list - https://tripl.example.com (from $TRIPL_BASE_URL)

prod (Prod)
  drift-1  app.screen_view.cart_value  type_changed  open  detected 2026-07-28T04:10:00Z
  drift-3  app.purchase.coupon_code    new_field     open  detected 2026-07-30T22:41:00Z

mobile (Mobile)
  (no drifts)

2 drifts in 2 projects, 2 untriaged.
```

The columns are the drift id, `<event type>.<field>`, the drift type, the
status, and the timestamp that matters for *that* status — when a snooze
**lapses** for a `snoozed` row, when it was **detected** for everything else.
The date an operator acts on is the one that gets printed.

:::warning There is no project-level drift endpoint
The API answers drifts **per event type**, so "this project's drifts" is a
fan-out — one request each — which is why there is a budget at all, and why
"could not read one event type" is a case that has to exist. It is reported as a
line, an `errors[]` entry and **exit 1**, never as a shorter list at exit 0:

```text
prod (Prod)
  /projects/prod/event-types: unavailable (Forbidden (403): the API key lacks the required scope)

0 drifts in 1 project, 0 untriaged.
1 read failed; the list above is incomplete.
```

`0 drifts` and `1 read failed` on the same screen is the whole design. Never
read the first line without the second.
:::

`--max-event-types` is spent **round-robin across the selected projects**, not
in project order, so one large project cannot consume the whole budget and leave
another at zero reads with nothing said about it. What the budget did not reach
is reported per project:

```text
  183 of 240 event types examined; raise --max-event-types to look at the rest.
```

This is the same planner doctor's `drifts` check uses, from the same function —
two implementations of a budgeted fan-out is how *"we did not look there"* starts
printing as *"nothing there"*.

:::note `--status` is filtered by this CLI, not by the API
The drifts endpoint has no status parameter, so every status is fetched and the
filter is applied locally. Two consequences. The request cost does not change
with `--status`. And `untriaged` is a **rule, not a status**: `open`, or
`snoozed` with a `snoozed_until` that has already passed. A snooze that has
lapsed is untriaged again, which is exactly what makes a snooze safe.

The footer's `N untriaged` counts the rows **on screen**. Under the default it
therefore always equals the drift count; under `--status false_positive` it is
always 0. It tells you something only under `--status all`.
:::

**Cost:** one request for the project listing (or one per named `--project`
slug), one per project for its event types, then one per event type actually
examined.

### `tripl drifts dismiss`

```
usage: tripl drifts dismiss [-h] [--url URL] [--api-key KEY] [--config PATH]
                            [--project SLUG] [--snooze-until TS] [--note TEXT]
                            [--dry-run] [--yes] [--json] [--timeout SECONDS]
                            <drift-id>
```

| Flag | Meaning |
|------|---------|
| `<drift-id>` | The drift to dismiss. Take it from the first column of `drifts list`. |
| `--project SLUG` | **Required**, exactly once — the action route carries a slug that a drift id cannot supply. |
| `--snooze-until TS` | RFC 3339. Its **presence selects `snooze`**; its absence selects `false_positive`. |
| `--note TEXT` | Resolution note stored with the drift. The server caps it at 2000 characters. **Replaces** the stored note; omitting it leaves the stored note untouched — see below. |
| `--dry-run` | Resolve everything, print the request, send nothing. Never prompts. |
| `--yes` | Skip the confirmation. **Required when stdin is not a terminal.** |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

**Write. Needs a `tk_w_` key backed by an editor or owner. Prompts.**

```bash
tripl drifts dismiss drift-1 --project prod --yes
```

```text
tripl drifts dismiss - https://tripl.example.com (from $TRIPL_BASE_URL)

prod: drift drift-1 (cart_value, type_changed) is now false_positive.
```

```bash
tripl drifts dismiss drift-1 --project prod \
  --snooze-until 2026-08-04T00:00:00Z \
  --note 'waiting on the mobile release' --yes
```

```text
tripl drifts dismiss - https://tripl.example.com (from $TRIPL_BASE_URL)

prod: drift drift-1 (cart_value, type_changed) is now snoozed.
```

There is **no `--action` flag**, and that is the safety design rather than an
omission: the only two actions reachable are `false_positive` and `snooze`, and
which one you get is decided by whether you passed a timestamp. This command
never sends `accept` — see
[what is deliberately not here](#what-is-deliberately-not-here). Undoing a
dismissal is [`tripl drifts reopen`](#tripl-drifts-reopen), a verb of its own,
because it moves the drift the other way and destroys something on the way.

:::note `--note` overwrites; omitting it keeps the note already on the drift
The action route writes `resolution_note` **only when the request carries a
note**, so a drift dismissed last week with
`--note 'waiting on the mobile release'` and snoozed again today without
`--note` keeps last week's sentence. This has not always been true: the route
used to assign the note on every call, so an action without one erased the
reason. A runbook that tells you to re-pass `--note` on every dismiss to avoid
losing it is describing the old defect and no longer buys anything.

What remains true is that `--note TEXT` **replaces** whatever was stored — there
is no append, and the drift keeps exactly one note. Read the current one out of
`tripl drifts list --json` before you overwrite a note you did not write;
`resolution_note` is carried there verbatim. The only way to blank a note from
here is `--note ''`, which stores an **empty** note rather than removing it.

This CLI omits `note` from the request body entirely rather than sending `null`
— `--dry-run` prints the exact body — so the paragraph above holds however a raw
client's explicit `null` is read. The one action that clears a note
unconditionally is `reopen`, and it is not reachable from this command — it is
[a verb of its own](#tripl-drifts-reopen), which is why it takes no `--note`.
:::

:::warning A naive `--snooze-until` is read as **UTC**, not as your local time
`--snooze-until '2026-08-04T00:00:00'` means midnight UTC. A snooze silently
shifted by your machine's offset is a drift that reappears at the wrong hour, so
the CLI picks the unambiguous reading and the API stores an aware datetime
either way. Pass the offset explicitly (`...T00:00:00Z`, `...+02:00`) if you
care. A value that is not RFC 3339 fails at parse time, exit 2, before a socket
opens.

A `--snooze-until` in the **past** is accepted by the server and the CLI will
report `is now snoozed` — but a lapsed snooze counts as untriaged, so the drift
is back in `drifts list` and in doctor's count immediately. Check the timestamp.
:::

**Cost:** one request, or zero with `--dry-run` — the drift id and the slug come
straight from your arguments, so there is nothing to resolve first. The
corollary is that a wrong `<drift-id>` is discovered by the server, as a 404 and
exit 1, not by the CLI.

### `tripl drifts reopen`

```
usage: tripl drifts reopen [-h] [--url URL] [--api-key KEY] [--config PATH]
                           [--project SLUG] [--dry-run] [--yes] [--json]
                           [--timeout SECONDS]
                           <drift-id>
```

| Flag | Meaning |
|------|---------|
| `<drift-id>` | The drift to reopen. Take it from the first column of `drifts list`. |
| `--project SLUG` | **Required**, exactly once — the action route carries a slug that a drift id cannot supply. |
| `--dry-run` | Resolve everything, print the request, send nothing. Never prompts. |
| `--yes` | Skip the confirmation. **Required when stdin is not a terminal.** |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

**Write. Needs a `tk_w_` key backed by an editor or owner. Prompts.**

The undo for a dismissal that turned out to be wrong: the drift goes back to
`open`, its snooze is cleared, and it counts as untriaged in `tripl doctor`
again.

```bash
tripl drifts reopen drift-1 --project prod --yes
```

```text
tripl drifts reopen - https://tripl.example.com (from $TRIPL_BASE_URL)

prod: drift drift-1 (cart_value, type_changed) is now open.
```

:::warning Reopening **destroys** the resolution note and the resolver
This is not the harmless direction. The action route clears `resolution_note`,
`resolved_by` and `resolved_at` for `reopen` — so *who* dismissed this drift and
*why* are gone the moment the request lands, and dismissing it again does not
bring them back. Nothing in the API restores them.

If the sentence matters, read it out of `tripl drifts list --json` first;
`resolution_note` is carried there verbatim. That is also why there is **no
`--note`** here: the route clears the note before it looks at the request's, so a
flag would take a sentence, report success, and store nothing.
:::

There is no `--snooze-until` either — reopening clears the snooze rather than
setting one. To move a snooze, `dismiss` again with the new timestamp; that is
one request instead of two and never blanks the note in between.

**Cost:** one request, or zero with `--dry-run`.

## `tripl events`

`doctor`, `status`, `scans` and `drifts` all answer questions about the
*machinery*. `tripl events` and [`tripl plan`](#tripl-plan) answer questions
about the **content**: what is in the catalog, what shape is it declared to
have, and what changed on a branch.

```
usage: tripl events [-h] [--url URL] [--api-key KEY] [--config PATH] <verb> ...
```

Two verbs: `list` and `show`. A bare `tripl events` prints this group's help on
stderr and exits 2.

Both take **exactly one `--project`**. Every route here is per project and there
is no instance-wide form, so a repeated `--project` would be a fan-out this
command does not do — unlike `scans list` and `drifts list`, where repeating it
widens a real fan-out.

:::warning Read only, and staying that way
`POST /projects/{slug}/events` and `PATCH /projects/{slug}/events/{event_id}`
exist, and the shared request layer builds both — the MCP server's `create_event`
and `update_event` tools use them. There is no `tripl events create`.

A catalog write has to land on a **plan branch**: the MCP makes `branch_id`
required on both write tools precisely so an agent's draft never lands on the
live main plan by accident. Reproducing that gate from a shell means resolving a
branch, refusing main, prompting, and then reading the server's
canonical-name warnings back to you — because when a scan naming rule governs
the event type, the server derives the name from field values and may ignore the
one you sent. That is a command surface of its own. Shipping the easy half first
would leave the CLI with a write that is easier to get wrong than the agent's.

Until then: edit in the tripl app, or through the
[MCP server](../integrate/mcp-server.md).
:::

### `tripl events list`

```
usage: tripl events list [-h] [--url URL] [--api-key KEY] [--config PATH]
                         [--project SLUG] [--branch REF] [--search TEXT]
                         [--status STATUS] [--tag TAG] [--field-value TEXT]
                         [--meta-value TEXT] [--event-type ID]
                         [--silent-since-days N]
                         [--reviewed | --unreviewed]
                         [--offset N] [--limit N] [--order-by ORDER] [--json]
                         [--timeout SECONDS]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | **Required**, exactly once. |
| `--branch REF` | Read a plan branch instead of the live main plan. Name or id, matched exactly. |
| `--search TEXT` | Substring match over name and description. |
| `--status STATUS` | Lifecycle state. Repeatable, and the API keeps an event matching **any** of them. |
| `--tag TAG` | Exact tag match. |
| `--field-value TEXT` | Substring match on any field value — a screen name, typically. Case-insensitive. |
| `--meta-value TEXT` | Substring match on any meta value — a ticket key, typically. Case-insensitive, so `--meta-value TRIPL-4` also keeps `TRIPL-412`. |
| `--event-type ID` | Only events of this event type id, from `tripl plan types`. |
| `--silent-since-days N` | Only events the warehouse has not carried for N days, `0`–`3650`. |
| `--reviewed` / `--unreviewed` | Only events already marked reviewed, or only those not. Mutually exclusive; omitting both asks for either. Reviewing is a separate axis from lifecycle status, so `--reviewed` and `--status in_review` can both match the same event. |
| `--offset N` | Skip N events, to read the next page. Default `0`. |
| `--limit N` | How many events to ask for, `1`–`10000`, default `200`. |
| `--order-by ORDER` | Order the page: `catalog`, the authored catalog order, or `volume`, busiest-first by ingested volume over the last 24h. Unlike `--limit` this flag has **no** client-side default — omitting it leaves the parameter off the wire so the API's own default (`catalog`) applies. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

The lifecycle states are `draft`, `in_review`, `ready_for_dev`, `implemented`,
`live`, `deprecated` and `archived` — the API's own `EventStatus` enum, checked
against `backend/openapi.json` by a contract test rather than transcribed and
hoped over.

```bash
tripl events list --project prod --limit 2
```

```text
tripl events list - https://tripl.example.com (from $TRIPL_BASE_URL)

prod
  evt-1  app.screen_view.viewed  live   seen 2026-07-31T19:00:00Z  2 drifts
  evt-2  app.purchase.completed  draft  never seen

2 of 412 events shown; raise --limit or pass --offset to read the rest.
```

The columns are the event id, its name, its lifecycle state, **when the
warehouse last carried it**, and an open-drift count when there is one.
`never seen` is the row worth scanning for: an event that is declared,
implemented on paper, and has never actually arrived.

:::warning `2 events.` and `2 of 412 events shown.` are different sentences
A page that filled up and a catalog that ended print the **identical rows**. The
footer is the only thing that tells them apart, which is why `total` — the
count the API takes *before* paging — is read and reported rather than inferred
from the row count. It is the same class of mistake as reading a `404` as an
empty list, one level up: not "we could not look", but "we stopped looking and
said nothing".

`--offset 200` walks to the next page; `--limit 10000` asks for the lot in one
request. The JSON carries `total`, `offset`, `limit` and a derived `truncated`
so a consumer never has to do that arithmetic itself.
:::

:::note `--silent-since-days` is the one filter worth a runbook entry
`tripl events list --project prod --status live --silent-since-days 30` is the
list of events your plan says are **live** that nothing has sent for a month.
That is either a tracking regression nobody noticed or a plan entry nobody
retired, and both are worth knowing before a quarterly review rather than after.
:::

**Cost:** one request, plus one to resolve `--branch` when you pass it.

### `tripl events show`

```
usage: tripl events show [-h] [--url URL] [--api-key KEY] [--config PATH]
                         [--project SLUG] [--branch REF] [--json]
                         [--timeout SECONDS]
                         <event-id>
```

| Flag | Meaning |
|------|---------|
| `<event-id>` | The event to read. Take it from the first column of `events list`, or from `plan search`. |
| `--project SLUG` | **Required**, exactly once. |
| `--branch REF` | Read a plan branch instead of the live main plan. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

```text
tripl events show - https://tripl.example.com (from $TRIPL_BASE_URL)

prod
  evt-1  app.screen_view.viewed
    status       live
    reviewed     yes
    event type   app.screen_view (et-1)
    tags         checkout
    last seen    2026-07-31T19:00:00Z
    sunset       -
    drifts       2
    description  Fired when the checkout screen becomes visible
    fields
      screen_name  checkout
      cart_value   ${cart_value}
    meta (by definition id)
      mf-1  TRIPL-412
```

There is **no `<event-id>` name matching**, unlike `<scan>` and `<event-type>`.
Two events of different types may legitimately carry the same name, so there is
no rule that could refuse to guess without also refusing valid input. A wrong id
is discovered by the server, as a `404` and exit 1.

:::note Why this costs two requests
An event's `field_values` carry a `field_definition_id` and **nothing else** —
no name. So `events show` reads the event type's field definitions as well and
prints each value under the field name it sets. Without that second read the
`fields` block is a column of UUIDs, which is the part of this command worth
having, rendered useless.

The `meta` block does **not** get the same treatment, and says so in its
heading. Resolving a `meta_field_definition_id` needs the project's meta-field
catalog, and the shared request layer builds no route for it. A second spelling
of a REST path outside `tripl_cli.api` is exactly what the contract tests
forbid, so the ids are printed honestly instead. Read `--json` and join against
the app if you need the names.
:::

**Cost:** two requests, plus one to resolve `--branch` when you pass it.

## `tripl plan`

The event catalog says *what is tracked*. The plan says **what shape it is
declared to have**: which event types exist, what fields they require, which
`${variable}` placeholders are documented, and which branches carry unreviewed
changes.

```
usage: tripl plan [-h] [--url URL] [--api-key KEY] [--config PATH] <verb> ...
```

Five verbs: `types`, `fields`, `variables`, `branches` and `search`. A bare
`tripl plan` prints this group's help on stderr and exits 2. Every verb takes
exactly one `--project`, and every one except `branches` takes `--branch`.

:::note Why `plan types` and not `event-types list`
The grammar everywhere else is *`<plural-noun> <verb>`* — `scans list`,
`drifts dismiss`, `events show`. `plan types` bends it: `types` names a kind,
not an action.

The alternative was one top-level group per REST collection — `event-types
list`, `variables list`, `branches list`, `search` — which is four more entries
in `tripl --help` for four reads, and that is the same objection that rejected
`list-scans`. Nobody arrives at a terminal wanting to query the event-types
collection; they arrive wanting to know what the plan looks like. This is one
group per **question**, not one per collection.
:::

### The `--branch` flag

Every plan read is answered from exactly one revision. Without `--branch` that
is the live **main** plan; with it, the named branch.

```bash
tripl plan branches --project prod           # find the name
tripl plan fields app.purchase --project prod --branch checkout-redesign
```

`--branch` takes a **name or an id**, matched exactly — name first, then id,
never a substring and never case-insensitively. It is literally the matcher
`<scan>` uses, for the same reason: a `--branch` that silently resolved to the
wrong revision would answer plan questions about a plan you are not looking at,
and a read that is quietly wrong is worse than one that refuses. A selector
matching nothing exits 2 and lists the candidates.

:::note `--branch main` reads the live plan too
The API resolves main by the `?branch=` parameter being **absent**, and treats
main's own id, as `GET /branches` lists it, exactly the same. Any other
non-empty value must parse as a UUID belonging to the project or the request is
refused (`400` for a malformed id, `404` for one belonging to another project).
`--branch` is matched against that listing, where main appears under the name
`main`, a name no other branch can take. So `--branch main`, or main's id,
reads the live plan just as omitting the flag does.

Only the output tells the two apart. Without the flag it prints `prod` with no
branch named, and the `--json` document carries `"branch": null`. With
`--branch main` it prints `prod (branch 'main')` and carries main's id and name,
as it prints `prod (branch 'checkout-redesign')` for a working branch.

One consequence worth knowing: `?branch=` is contributed by a shared FastAPI
**dependency**, so it is declared on every route that takes plan context at once
and appears that way in `backend/openapi.json` and in generated clients. Its
schema there is a plain string rather than a UUID, because the dependency parses
the value itself — which is what makes a malformed id the `400` documented above
rather than FastAPI's own `422`.
:::

### `tripl plan types`

```
usage: tripl plan types [-h] [--url URL] [--api-key KEY] [--config PATH]
                        [--project SLUG] [--branch REF] [--json]
                        [--timeout SECONDS]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | **Required**, exactly once. |
| `--branch REF` | Read a plan branch instead of the live main plan. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

```text
tripl plan types - https://tripl.example.com (from $TRIPL_BASE_URL)

prod
  et-1  app.screen_view  Screen View  17 fields
  et-2  app.purchase     Purchase     9 fields

2 event types.
```

The route answers a bare array and pages nothing, so `total`, `offset` and
`limit` are all `null` in the JSON: there is no next page to miss. It is not
quite unlimited, though — the service applies a defensive cap of 1,000 event
types and reports nothing when it bites, so a project past that number sees a
silently short list here and in the app alike.

The field count is derived rather than served; the API embeds the whole
`field_definitions` array on every row. `tripl plan fields <event-type>` reads
one type's definitions.

**Cost:** one request, plus one to resolve `--branch` when you pass it.

### `tripl plan fields`

```
usage: tripl plan fields [-h] [--url URL] [--api-key KEY] [--config PATH]
                         [--project SLUG] [--branch REF] [--json]
                         [--timeout SECONDS]
                         <event-type>
```

| Flag | Meaning |
|------|---------|
| `<event-type>` | Event type **name or id**, matched exactly — name first, then id. |
| `--project SLUG` | **Required**, exactly once. |
| `--branch REF` | Read a plan branch instead of the live main plan. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

```text
tripl plan fields app.screen_view - https://tripl.example.com (from $TRIPL_BASE_URL)

prod (branch 'checkout-redesign')
  fd-1  screen_name  string  required  pii   enum: checkout|cart
  fd-2  cart_value   number  optional  none

2 fields.
```

The columns are the definition id, the field name, its type, whether it is
required, its sensitivity, and its enum options if it has any. This is what a
value has to satisfy — read it before writing `field_values` through the app or
an agent, and read it when a `enum_violation` drift appears and you want to know
what the declared set actually is.

The enum options go last and unabbreviated. The last column is never padded, so
a forty-value enum costs the rows around it nothing.

`<event-type>` is resolved **on the branch you named**, not on main: a type that
exists only on a branch has no id to find on main, and one renamed on a branch
would otherwise resolve against the wrong name.

**Cost:** two requests — the event-type listing, to resolve `<event-type>`, then
the fields — plus one to resolve `--branch` when you pass it.

### `tripl plan variables`

```
usage: tripl plan variables [-h] [--url URL] [--api-key KEY] [--config PATH]
                            [--project SLUG] [--branch REF] [--offset N]
                            [--limit N] [--json] [--timeout SECONDS]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | **Required**, exactly once. |
| `--branch REF` | Read a plan branch instead of the live main plan. |
| `--offset N` | Skip N variables, to read the next page. Default `0`. |
| `--limit N` | How many variables to ask for, `1`–`5000`, default `200`. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

```text
tripl plan variables - https://tripl.example.com (from $TRIPL_BASE_URL)

prod
  var-1  cart_value  number  12 events  1 open drift
  var-2  screen      string  40 events

2 variables.
```

Variables are the documented `${placeholder}` tokens an event name or field
value may carry. The columns are the id, the name, the declared type, how many
events use it, and its open **value drift** count — a variable observed carrying
a value outside its documented set. A non-zero count there is the same class of
signal `tripl drifts list` reports for schema, on the other axis.

The ceiling is `5000` rather than the events route's `10000` because that is
what the route enforces; a real project can carry well over a thousand
variables, so the default of `200` is a page and not the catalog.

**Cost:** one request, plus one to resolve `--branch` when you pass it.

### `tripl plan branches`

```
usage: tripl plan branches [-h] [--url URL] [--api-key KEY] [--config PATH]
                           [--project SLUG] [--json] [--timeout SECONDS]
```

| Flag | Meaning |
|------|---------|
| `--project SLUG` | **Required**, exactly once. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

**No `--branch`.** The branch listing is a property of the project, not of a
revision, so there is no `?branch=` for it to send and a flag that quietly did
nothing would read as one that did something.

```text
tripl plan branches - https://tripl.example.com (from $TRIPL_BASE_URL)

prod
  b-0001  main               main     merged  -
  b-9f21  checkout-redesign  working  draft   3 ahead  behind base

2 branches.
```

The columns are the branch id, its name, its kind (`main` or `working`), its
status (`draft`, `ready_for_review`, `changes_requested`, `approved`, `merged`,
`closed`), how many changes it is ahead of its base, and whether its **base has
moved under it**. Both are computed for open branches only: a `merged` or
`closed` row shows `-` and no `behind base`, like `main`.

`behind base` is the one to act on. A branch whose base has moved has a diff
that no longer describes what merging it would do — rebase it in the app before
anyone reviews it.

This command exists mostly to give you the name to pass to `--branch`.

:::note What is deliberately not here
`tripl plan diff` is **not** shipped, though the route exists and the MCP
server's `get_branch_diff` tool reads it. The diff answers
`{behind_base, entries, renames, summary}` — not a list — so it does not fit the
one document shape the seven read verbs share, and `behind_base` is too
load-bearing to smuggle into an `items` array. `renames` makes that worse rather
than better: it is a second top-level key, and one that points *at* `entries` —
each pair naming the removal and the addition that are really one renamed row,
which a merge applies as a single in-place update — so flattening the document
to a list would drop the pairing on the floor. A second document shape for one
verb is worse than not shipping it yet.

Merging, reverting and branch transitions are not here either, and never will
be: both surfaces hand that decision back to a human in the tripl app, which is
where the conflict resolution and the review state live.
:::

**Cost:** one request.

### `tripl plan search`

```
usage: tripl plan search [-h] [--url URL] [--api-key KEY] [--config PATH]
                         [--project SLUG] [--branch REF] [--type TYPE]
                         [--limit N] [--json] [--timeout SECONDS]
                         <query>
```

| Flag | Meaning |
|------|---------|
| `<query>` | Phrase or partial name, 1–500 characters. Stripped, and a blank one is exit 2. |
| `--project SLUG` | **Required**, exactly once. |
| `--branch REF` | Read a plan branch instead of the live main plan. |
| `--type TYPE` | Restrict to one entity kind. Repeatable. |
| `--limit N` | How many hits to ask for, `1`–`100`, default `20`. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

The entity kinds are `event`, `event_type`, `field`, `meta_field`, `variable`,
`relation`, `tag`, `metric`, `fact_table`, `scan_config` and `alert_rule` — the
API's own list, pinned to `backend/openapi.json` by a contract test.

The last two are project configuration rather than plan content, and each is
indexed with the text you would look for it by: a scan carries the columns it is
wired to and the warehouse SQL it runs, so `--type scan_config` answers "which
scan reads this table"; an alert rule carries its message and item templates.

```bash
tripl plan search 'checkout funnel' --project prod --type event --limit 2
```

```text
tripl plan search - https://tripl.example.com (from $TRIPL_BASE_URL)

prod
  event  evt-1  app.purchase.completed  Purchase  0.92
  event  evt-7  app.checkout.started    Checkout  0.61

2 search results shown — the most this page holds, and more may have matched; raise --limit.
```

The columns are the entity kind, its **id**, its title, its subtitle and a
confidence in `[0, 1]`. The id is in the table rather than only in the JSON
because it is the argument the follow-up takes: search, then read the entity by
id with `tripl events show` or `tripl plan fields`.

:::warning Search is capped by `--limit` alone
There is no `--offset`, because the route has no offset parameter — a hit past
`--limit` is unreachable except by raising it, and the truncation line says
exactly that rather than advising a flag that cannot exist. The JSON reports
`"offset": null` for the same reason.

`"total"` is `null` too, and that is not an omission: the route computes its
total **after** trimming to the limit, so it always equals the page and can
never say how many matched. Reporting it would have read `2 of 2 search results
shown` on a search that dropped hits. The warning above comes from the route's
own `truncated` instead — search retrieves one row past the window it ranks, so
it reports whether a hit was dropped rather than inferring it from a page that
happens to be full. That inference survives only as the fallback for an instance
too old to report the flag. Either way the line warns without a number, because
there is no count to warn with.
:::

:::note Read `semantic_used` before you trust a low score
The `--json` document carries `meta.semantic_used`. When it is `false` the
instance answered from substring matching rather than the semantic index — the
scores mean something different, and a phrase that would have matched
semantically may be missing entirely. A ranking consumer that ignores this will
silently change behaviour the day the index is rebuilt.
:::

**Cost:** one request, plus one to resolve `--branch` when you pass it.

## `tripl annotate`

Posts one marker onto every monitoring (Volume tab) chart in a project — "Deployed web 2026.09.25",
with a link to the release — so the next anomaly on a chart sits next to the
deploy that probably caused it. It is written for a **CI step**: one request,
no prompt, and safe to retry.

```
usage: tripl annotate [-h] [--base-url URL] [--api-key KEY] [--config PATH]
                      [--project SLUG] [--url URL] [--at TIMESTAMP]
                      [--description TEXT]
                      [--scope-type {project_total,event_type,event,metric}]
                      [--scope-ref REF] [--dry-run] [--json]
                      [--timeout SECONDS]
                      <label>
```

| Flag | Meaning |
|------|---------|
| `<label>` | The text drawn on the chart, 1–200 characters. |
| `--project SLUG` | **Required**, exactly once. |
| `--url URL` | The release or pull-request link the chart tooltip opens. `http` or `https`, at most 500 characters. |
| `--at TIMESTAMP` | When it happened, RFC 3339 (`2026-09-25T14:02:00Z`). A value with no offset is read as UTC. Default: now. |
| `--description TEXT` | Longer text for the tooltip, up to 2000 characters. |
| `--scope-type TYPE` | Draw it only on charts of this scope: `project_total`, `event_type`, `event` or `metric`. Needs `--scope-ref`. Omit both for a project-level marker, which every monitoring (Volume tab) chart in the project shows. |
| `--scope-ref REF` | The event type, event or metric the scope names. Needs `--scope-type`. |
| `--dry-run` | Build the request, print it, send nothing. |
| `--json` | One JSON document on stdout, every human line on stderr. |
| `--timeout SECONDS` | Per-request timeout, default `10.0`, range 0.1–600. |

**Write. Needs a `tk_w_` key backed by an editor or owner. Does not prompt, and
has no `--yes`.**

:::warning `--url` here is the link, not the instance
On every other command `--url` is the instance URL. On `annotate` it is the
link the annotation carries, because that is the API field's name and the flag
every CI example reaches for. Give the instance with `$TRIPL_BASE_URL` (the
usual case in CI), `--base-url`, or `--url` placed **before** the command name:
`tripl --url https://tripl.example.com annotate ...`.
:::

```bash
tripl annotate "Deployed web 2026.09.25" --project prod \
  --url https://github.com/acme/web/releases/tag/2026.09.25
```

```text
tripl annotate - https://tripl.example.com (from $TRIPL_BASE_URL)

prod: annotated 'Deployed web 2026.09.25' at 2026-09-25T14:02:00Z (ann-7f3a).
```

The annotation is created with **source `api`**. Charts draw it muted, with a
rocket icon, and hide it together with the automatic release markers when a
viewer turns **Show releases** off — see
[Chart annotations](../use/feature-reference.md#chart-annotations).

### A `200` means it was already there

The API **de-duplicates** `api` annotations: the same label with source `api`,
posted to the same project within 24 hours, returns the annotation that already
exists — with
a `200` instead of a `201` — rather than drawing it twice. A re-run deploy job
therefore leaves one marker, not two. (Only `api` and the worker's `release`
markers are de-duplicated; `manual` annotations from the app's form never are.)
`annotate` still exits **0**, and says which of the two happened:

```text
tripl annotate - https://tripl.example.com (from $TRIPL_BASE_URL)

prod: annotation 'Deployed web 2026.09.25' already exists (ann-7f3a, at 2026-09-25T14:02:00Z); the API de-duplicated it and nothing new was created.
```

Under `--json` the same fact is `"deduplicated": true` in the
[mutation document](#mutation-documents). Put something unique in the label —
the version, the commit — if two deploys a day are two separate events.

### In a GitHub Actions workflow

```yaml
- name: Mark the deploy on tripl charts
  env:
    TRIPL_BASE_URL: https://tripl.example.com
    TRIPL_API_KEY: ${{ secrets.TRIPL_WRITE_KEY }}
  run: >
    uvx tripl annotate "Deployed web ${{ github.ref_name }}" --project prod
    --url "${{ github.server_url }}/${{ github.repository }}/releases/tag/${{ github.ref_name }}"
```

The same request without the CLI is in the
[Agent API guide](../integrate/agent-api-guide.md#chart-annotations).

**Cost:** one request, or none with `--dry-run`. The slug is not resolved
first: a wrong one comes back as the route's own `404`.

## `tripl install`

Every command above needs an instance to talk to. This is the one that **makes
one**. It is the executable form of
[Self-hosting & Deployment](./deployment.md#install-with-the-cli) — same compose
file, same variables, same `pull` and `up -d` — with the secret generation and
the file permissions done for you instead of copied out of a code block.

```
usage: tripl install [-h] [--url URL] [--api-key KEY] [--config PATH]
                     --app-url URL [--dir PATH] [--version TAG]
                     [--wait SECONDS] [--no-start] [--force] [--dry-run]
                     [--yes] [--json]
```

| Flag | Meaning |
|------|---------|
| `--app-url URL` | **Required, no default.** The public origin of the instance you are creating, e.g. `https://tripl.example.com`. Becomes `APP_BASE_URL`, which drives CORS and cookies. A trailing `/` or a pasted `/api/v1` is trimmed. |
| `--dir PATH` | Where the stack lives. Default `./tripl`; always **reported absolute**, whatever you typed. |
| `--version TAG` | Image tag to pin in `.env`. Default `latest`. Must look like a Docker tag — a letter, digit or underscore followed by up to 127 of `[A-Za-z0-9._-]`. |
| `--wait SECONDS` | How long to poll `/health` before giving up. Default `300`, range `0`–`3600`. **`0` skips waiting entirely** — it does not mean "probe once". |
| `--no-start` | Write the files and run nothing. Also skips the Docker probe, so it works on a machine with no Docker at all. |
| `--force` | Replace a `compose.yaml` or `rabbitmq.conf` that differs from the packaged one. **Never reaches `.env`.** |
| `--dry-run` | Print the plan; write nothing, run nothing. |
| `--yes` | Skip the confirmation prompt. Required when stdin is not a terminal *and* a prompt would be asked — see [when it asks](#install-only-asks-in-one-case). |
| `--json` | One JSON document on stdout, every human line on stderr. |

:::note `--url` and `--api-key` are inherited, and refused
Both appear in the usage line because every subcommand shares one parent parser.
Passing either **explicitly** to `install` or `upgrade` is **exit 2**, with a
message naming `--app-url` instead. These commands act on a directory; a
connection flag that was silently ignored is how an operator provisions the wrong
box and finds out later.
:::

```bash
tripl install --app-url https://tripl.example.com --version 1.5.0 --dir /srv/tripl
```

`--dry-run` prints exactly what a real run would do, and is worth typing first.
Here without `--version`, so the plan shows the `latest` default:

```bash
tripl install --app-url https://tripl.example.com --dir /srv/tripl --dry-run
```

```text
tripl install - /srv/tripl

app url  https://tripl.example.com
image    ghcr.io/vladenisov/tripl:latest

files
  compose.yaml                  create  0644
  infra/rabbitmq/rabbitmq.conf  create  0644
  .env                          create  0600
  generated into .env, values never printed: ENCRYPTION_KEY, SECRET_KEY, POSTGRES_PASSWORD, RABBITMQ_PASSWORD

commands
  + cd /srv/tripl && docker compose pull
  + cd /srv/tripl && docker compose up -d
  then poll https://tripl.example.com/health for up to 300s

dry run: nothing was written and nothing was run.
```

That is stdout in full. Two more lines can land on **stderr**, and both are
warnings rather than refusals, because only you know whether they apply:

- Leaving `--version` at `latest` prints a reminder to pin a released tag in
  production, since `latest` follows every release and a re-run would move you.
- An `http://` `--app-url` prints a warning that the stack forces
  `SESSION_COOKIE_SECURE=true`, so a browser will not store the session cookie
  over plain HTTP — Safari refuses it even on `localhost` — and nobody will stay
  signed in. Legitimate if you terminate TLS elsewhere; otherwise re-run with an
  `https` URL.

The two `+ cd ... && docker compose ...` lines are printed before each command
runs, in a real run too. They are shell-quoted, so they are safe to paste — that
is the point of printing them.

### What it writes

Three files, and only three.

| Path (under `--dir`) | Mode | Contents |
|----------------------|------|----------|
| `compose.yaml` | `0644` | A **verbatim copy** of the production compose file this repository deploys, minus one block — see below. |
| `infra/rabbitmq/rabbitmq.conf` | `0644` | A verbatim copy. Not optional: `compose.yaml` bind-mounts it, and Docker's answer to a missing bind-mount source is to create a *directory* there, after which RabbitMQ fails to start with an error naming neither tripl nor the mount. |
| `.env` | `0600` | Generated. The only file that holds secrets. |

There is deliberately **no data directory**. PostgreSQL lives in the named volume
`pgdata18` and uploaded event photos in the named volume `photos`, so backing up
`--dir` backs up your configuration and **none of your data**.

Each file gets one of five actions, and the same five words appear in the human
table and in the `--json` document:

| Action | Meaning |
|--------|---------|
| `create` | It did not exist. Created with the mode above. |
| `unchanged` | It exists and is byte-identical to the packaged copy. Nothing is opened. |
| `kept` | It exists and **differs**. Yours is kept; the table says so. `--force` turns this into `replace` — for `compose.yaml` and `rabbitmq.conf` only. |
| `replace` | Written beside the target and renamed over it, so a reader never sees a half-written file. |
| `append` | `.env` only. New keys are appended under a dated comment; **nothing already in the file is changed**. |

The packaged `compose.yaml` differs from the repository's in exactly one way: the
`mcp` service's `build:` block is removed, because a fresh host has no source
tree and modern compose *builds* when an image is absent locally. `--profile mcp`
therefore pulls the published `tripl-mcp` image rather than trying to build it. A
contract test asserts the rest is byte-identical, so the file you get is the file
this project deploys.

### The generated `.env`

```text
# tripl instance configuration, generated by `tripl install` on 2026-08-01T09:00:00Z.
#
# Docker Compose reads this file to INTERPOLATE compose.yaml. The containers receive
# only the variables compose.yaml lists in its environment map, so a variable added
# here that compose.yaml does not mention reaches nothing. Every tunable is listed at
# https://vladenisov.github.io/tripl/run/configuration
#
# This file holds live secrets: mode 600, and it must stay out of version control.
# Back up ENCRYPTION_KEY separately from the database - warehouse and alert-
# destination credentials are encrypted with it and cannot be recovered without it.

# Public origin of this instance. Drives CORS, and must be the exact origin a browser
# reaches. The stack forces SESSION_COOKIE_SECURE=true, so this should be https.
APP_BASE_URL=https://tripl.example.com

# Released image and tag. `tripl upgrade --to X.Y.Z` moves the tag.
TRIPL_IMAGE=ghcr.io/vladenisov/tripl
TRIPL_VERSION=1.5.0

# Generated secrets - do not edit by hand.
# ENCRYPTION_KEY is a Fernet key: 32 random bytes, url-safe base64.
ENCRYPTION_KEY=...
SECRET_KEY=...
POSTGRES_PASSWORD=...
RABBITMQ_PASSWORD=...
```

Seven variables, and that is the whole file. It is **not** a copy of
`.env.example`, which is the backend *development* template and is full of
`localhost` URLs and keys `compose.yaml` never reads.

| Variable | How it is produced |
|----------|--------------------|
| `APP_BASE_URL` | Your `--app-url`, normalised. |
| `TRIPL_IMAGE` | `ghcr.io/vladenisov/tripl`. |
| `TRIPL_VERSION` | Your `--version`, default `latest`. |
| `ENCRYPTION_KEY` | 32 random bytes, url-safe base64 — 44 characters. This is exactly what a **Fernet** key is, which is what the backend builds from it at startup. |
| `SECRET_KEY` | `secrets.token_urlsafe(48)` — 64 url-safe characters. |
| `POSTGRES_PASSWORD` | `secrets.token_hex(24)` — 48 hex characters. |
| `RABBITMQ_PASSWORD` | `secrets.token_hex(24)` — 48 hex characters. |

The two passwords are **hex on purpose**, not for looks. `compose.yaml` builds
`postgresql+asyncpg://tripl:${POSTGRES_PASSWORD}@postgres:5432/tripl` by plain
string interpolation, so a base64 password containing `/`, `+`, `@` or `:`
corrupts the URL into something that fails to connect with an error naming
neither the password nor the encoding. Hex has no such character. Neither
password can come out as `tripl` or `guest`, the dev defaults the backend's
`assert_production_ready()` refuses outright.

:::danger No generated value is ever printed, and you should keep it that way
The plan, the human output and the `--json` document name the **keys**
(`secrets_generated`) and never the values. A secret printed to a terminal is a
secret in a scrollback buffer, in a `tee` log, and in whatever gets pasted into a
ticket. If you need to read one, read the `0600` file.

`ENCRYPTION_KEY` is the irreplaceable one, and backing it up is a deployment
step rather than a CLI one:
[treat ENCRYPTION_KEY as irreplaceable](./deployment.md#the-variables-the-stack-needs).
:::

### Four rules about writing

1. **`.env` is never overwritten.** It is created, or appended to with your
   confirmation, or left alone. `--force` does not reach it and there is no flag
   that does.
2. **`.env` and `.env.bak.*` are created at `0600` by the `open` call itself**,
   never opened and then `chmod`-ed — that would leave a window, however short,
   in which the database password and `SECRET_KEY` were world-readable. If an
   *existing* `.env` is readable by other users, you get a warning on stderr and
   a `chmod 600` to run; the CLI will not change the mode of a file it did not
   create.
3. **A `compose.yaml` that differs from ours is yours.** It is reported and kept.
4. **The version pin is rewritten by copy-then-replace**, preserving every other
   byte: comments, blank lines, key order and the trailing newline.

`install` also **refuses to run against a tripl source checkout** — a directory
containing `.git` or `backend/src/tripl` — because it writes its own
`compose.yaml` and would land next to the real one. That is exit 2, and the fix
is one `--dir`.

### `install` only asks in one case

A fresh install **does not prompt**. The single confirmation is for an existing
`.env` that is missing some required keys: it names them, says nothing already in
the file will change, and appends under a dated comment. That is where `--yes`
matters — and where a non-TTY without `--yes` is exit 2, as everywhere else in
this CLI.

Consequence worth knowing for a provisioning script: `tripl install` in a
pipeline needs `--yes` **only** if it might meet a half-written `.env`. Pass it
anyway; the alternative is a job that works until the day it does not.

### Re-running is the supported way to converge

`install` is idempotent, not one-shot. A second run against the same directory
reports `unchanged` / `kept` for the files, leaves `.env` alone, and **still runs
`pull` and `up -d`**, because "make the running stack match what is on disk" is
the useful meaning of a re-run.

### What actually runs, and where its output goes

```text
+ cd /srv/tripl && docker compose pull
+ cd /srv/tripl && docker compose up -d
```

No `-f compose.yaml` and no `--project-directory`, on purpose: that is compose
invoked exactly the way you would [by hand](./deployment.md#bring-it-up), and
that page gives the reasons.

Before writing anything, `install` checks for `docker` on `PATH`, then for
`docker compose version`, then for a reachable daemon, and stops at the first
failure with the daemon's **own stderr** echoed under a `docker:` prefix. A
legacy `docker-compose` v1 binary is *detected and named* in the error but never
used: the compose file relies on `depends_on.condition`, which v1 ignores, so the
schema migration would race the app instead of gating it. All three checks are
skipped under `--no-start`.

:::warning compose's output is inherited, never captured
`pull` and `up -d` write straight to your terminal — layer progress, and the one
line that names a failing service. Nothing wraps them. The cost is that their
output **cannot** appear in the `--json` document, which carries the `argv`, the
`cwd`, the env overlay and the `returncode` of each command instead. If you need
the text, it is in your terminal or in the `docker compose logs` the failure
message points you at.
:::

### Waiting for `/health`

`docker compose up -d` returns as soon as the containers are *created*, which on
a first run is a minute or more before the app answers: the `migrate` one-shot
still has to apply every Alembic revision. So `install` polls
`<--app-url>/health` **every 5 seconds** until it answers or `--wait` runs out.

:::warning It polls the public URL, so TLS has to be there already
The poll goes to the origin you passed as `--app-url` — not to
`http://localhost:8000`. If your reverse proxy is not in front of port `8000`
yet, `https://tripl.example.com/health` will not answer no matter how healthy the
stack is, and `install` reports a timeout and exits **1** after `--wait`
seconds.

That is a sequencing problem, not a failed install. Either put TLS up first, or
run `tripl install --wait 0`, bring the proxy up, and check by hand:

```bash
curl -fsS http://127.0.0.1:8000/health     # from the deploy host
```
:::

A timeout prints where to look, and does not roll anything back — the stack **is**
started:

```text
The stack was started; it just has not answered yet. A first run applies every
Alembic revision before the app boots, which on a slow disk can outlast the
default wait. Look here, in this order:
  cd /srv/tripl && docker compose ps
  cd /srv/tripl && docker compose logs migrate
  cd /srv/tripl && docker compose logs app
  tripl doctor --url https://tripl.example.com
```

:::note That last line needs an API key you do not have yet
`tripl doctor` demands both a URL **and** a key before it opens a socket, even
though `/health` itself needs neither. On a brand-new instance there is no
account, so there is no key. Come back to that line after you have created the
owner account and a `tk_r_` key — the three `docker compose` lines above it are
the ones that work right now.
:::

### What `install` deliberately does not do

It brings up a **running, empty instance**. It does not create the owner account,
it does not connect a warehouse, and it does not run the first scan. Two of those
three are unreachable over the API at all, and the third depends on them:

- **A data source cannot be created with an API key, of any scope, held by any
  role.** `POST /data-sources` requires an interactive **owner session**; a
  request carrying an API key scope is `403` by construction. Same wall that
  keeps [`tripl scans replay`](#what-is-deliberately-not-here) out of this CLI.
- **An owner account could be registered over the API, and should not be.** It
  would mean accepting a password on an argv — visible through `ps(1)`, kept in
  shell history — and the session cookie the reply sets is `Secure`, so over a
  plain-HTTP first run the cookie is discarded and the "automated" flow breaks
  silently.

So the last thing `install` prints is the truth about **your** instance, read
from the unauthenticated `/auth/status`:

```text
This instance has no accounts yet. Open https://tripl.example.com and create the
first one - it becomes the owner.
Then, signed in as that owner:
  Settings -> Data sources   connect ClickHouse, BigQuery or PostgreSQL. Owner-only,
                             and only from a browser session: an API key cannot reach
                             this endpoint whatever its scope.
  Settings -> API keys       create a tk_r_ key for the CLI.
Then: tripl doctor --url https://tripl.example.com
```

If the instance already has accounts it says so instead, and if registration is
closed it points at **Settings → Members → Invite a member**. If `/auth/status`
could not be read it says *that*, and falls back to the general wording — it never
guesses. See [Members](../administer/admin-guide.md#members) for what happens on
that first sign-in, and
[Connecting a warehouse](./deployment.md#connecting-a-warehouse) for the step
after it.

## `tripl upgrade`

The most dangerous thing this CLI does, and it is built like it. Moving to a new
tag applies Alembic migrations, and those are **not reversible here**.

```
usage: tripl upgrade [-h] [--url URL] [--api-key KEY] [--config PATH] --to TAG
                     [--dir PATH] [--wait SECONDS] [--dry-run] [--yes]
                     [--json]
```

| Flag | Meaning |
|------|---------|
| `--to TAG` | **Required.** The tag to move to, e.g. `1.5.0`. There is no default and no "upgrade to latest" convenience. |
| `--dir PATH` | Where the stack lives. Default `./tripl`. Must already contain `compose.yaml` **and** `.env`, or it is exit 2 naming `tripl install`. |
| `--wait SECONDS` | As for `install`: default `300`, range `0`–`3600`, `0` skips. |
| `--dry-run` | Print the plan; write nothing, run nothing. |
| `--yes` | Skip the confirmation. **`upgrade` always confirms**, so a non-interactive run always needs this. |
| `--json` | One JSON document on stdout, every human line on stderr. |

```bash
tripl upgrade --to 1.5.0 --dir /srv/tripl --dry-run
```

```text
tripl upgrade - /srv/tripl

from  1.4.0
to    1.5.0

commands
  + cd /srv/tripl && TRIPL_VERSION=1.5.0 docker compose pull
  + cd /srv/tripl && docker compose up -d
  the 1.5.0 pin is written to .env between the two, after the pull succeeds
  then poll /health for up to 300s

dry run: nothing was written and nothing was run.
```

The current tag is read out of `.env`, and so is the origin to poll — you set
`APP_BASE_URL` at install time and being asked for it again is one more chance to
type a different one, which is how a CORS mismatch gets introduced during an
upgrade. If `.env` carries no `APP_BASE_URL`, the health wait is reported as
skipped rather than run against a guess.

### How the two tags are compared

| `ordering` | When | What happens |
|------------|------|--------------|
| `same` | The pin already equals `--to`. | `already at X; nothing to do.` **Exit 0**, nothing run, `.env` untouched — a converging provisioning script must not be a failing one. |
| `upgrade` | Both are strict `X.Y.Z` and the target is higher. | Proceeds to the backup gate. |
| `downgrade` | Both are strict `X.Y.Z` and the target is lower. | **Refused outright, exit 2, no override flag.** |
| `unknown` | Either side is not a strict `X.Y.Z` — `latest`, `1.4`, `sha-abc1234`. | Prints the plan, then **refuses without `--yes`**, exit 2. With `--yes` it proceeds and warns that this may be a downgrade. |

```text
tripl: downgrade refused: 1.4.0 -> 1.3.0. Alembic migrations are not reversible here, so the older image cannot read the schema the newer one wrote. Restore your backup instead. There is no override flag.
```

There is no override because there is no safe one. Once `alembic upgrade head`
has run, the old image does not know how to read the new schema, and the honest
instruction is *restore your backup*.

### The backup gate

`upgrade` **prints** the `pg_dump` command and refuses to continue without an
acknowledgement. It never runs it:

```text
backup   cd /srv/tripl && docker compose exec -T postgres \
           pg_dump -U tripl tripl | gzip > tripl-1.4.0.sql.gz
         Take it now. This applies Alembic migrations, which are not reversible.
         ENCRYPTION_KEY lives in .env, not in that dump - back it up separately.
```

A dump this tool invoked and then called "your backup" would be a promise it
cannot keep: it cannot know there is disk space, it cannot verify the dump, and
**the dump does not contain `ENCRYPTION_KEY`** — without which every encrypted
column in it is unreadable.

The prompt names the directory's basename, which is also the compose project
name:

```text
Have you taken that backup of tripl? This upgrades 1.4.0 -> 1.5.0 and applies migrations that cannot be undone. [y/N]
```

Anything but `y`/`yes` aborts with exit 1, and on a non-TTY without `--yes` it is
exit 2 — the same rule as every other prompt on this page.

### Order of operations, and why

```text
pull  ->  write the pin  ->  up -d
```

The **pull comes first** so a bad tag or an unreachable registry leaves `.env`
untouched; the failure message says so explicitly. The pin is written to `.env`
**before** `up -d` rather than passed inline, because an inline
`TRIPL_VERSION=x docker compose pull` applies to the pull only and the following
`up` would start `${TRIPL_VERSION:-latest}` — the exact trap this ordering
exists to avoid. That is why the printed pull line carries the assignment inside
the `cd`, and the `up -d` line does not.

Rewriting `.env` first copies it to `.env.bak.<UTC>` at `0600` and only then
moves the pin, preserving every other byte. That backup is created exclusively:
a second upgrade in the same second **refuses** rather than clobbering the only
copy of the pin you are moving away from.

`upgrade` does **not** run `alembic` and does **not** run
`docker compose run --rm migrate`. The `migrate` one-shot with
`condition: service_completed_successfully` is already the race-free mechanism;
a second CLI-owned migration path would be a second thing to keep in sync.

### When it fails

**The pull failed.** `.env` was never touched. You are still on the old tag and
the stack is still running the old image. Fix the tag or the registry access and
re-run.

**`up -d` failed.** The pin is left at the **new** tag, on purpose:

```text
The `migrate` one-shot runs `alembic upgrade head` before app and workers start,
so this most likely means the schema upgrade failed. Read it:
  cd /srv/tripl && docker compose logs migrate

TRIPL_VERSION is left at 1.5.0 on purpose. Alembic applies revisions one at a
time, so the schema may be partly upgraded; starting the old image against a
partly-new schema would be worse than leaving the stack stopped. Fix the cause and
re-run `tripl upgrade --to 1.5.0`, or restore the backup you took above.
The previous .env is at .env.bak.20260801T090000Z.
```

**`/health` never answered.** Same as for `install`, including the public-URL
trap: exit 1, nothing rolled back, and the `docker compose logs migrate` /
`logs app` lines to read.

## One request layer, shared with the MCP server

`tripl` and the [MCP server](../integrate/mcp-server.md) are two front ends over
one instance, and they now share more than an HTTP client. **Every REST path,
query parameter and response projection either surface uses is defined once**,
in `tripl_cli.api`, which `tripl-mcp` imports. Contract tests in both packages
fail the build if a path literal appears anywhere else.

That has consequences you can rely on:

- `tripl scans list` and the MCP `list_scans` tool return **the same trimmed
  scan-config shape**, including the derived `dispatchable` flag, computed by
  one function rather than two.
- `tripl scans run` and the MCP `trigger_scan` tool post to the same route, and
  cannot drift apart.
- `tripl drifts list` and doctor's `drifts` check spend the same budget with the
  same round-robin planner, and share one definition of *untriaged*.
- `tripl events list` and the MCP `list_events` tool send the same filters to
  the same route, and `tripl plan fields` and `get_event_type_fields` read the
  same two endpoints. What differs is the **projection**: the MCP trims each row
  to the handful of keys an agent needs, because a model pays for every token on
  every turn; the CLI passes rows through whole, because a pipe does not.
- Both surfaces resolve `--branch` / `branch_id` against the same branch
  listing, and `<scan>`, `<event-type>` and `--branch` all run through **one
  matcher** — exact on the name, then on the id — so no two arguments in this
  CLI can mean two different kinds of match.
- The job-history read is **one builder with four deliberate answers**: doctor
  asks for the maximum, 200, because it is measuring how long a streak has run;
  `watch` for 10, because it repeats every ten seconds; `scans jobs` for 50; and
  the MCP `get_scan_status` tool sends no limit at all and takes whatever the
  server's default is. Four different windows, one place they are spelled.

The practical rule for an operator: if `tripl` and an agent disagree about what
a project contains, it is not a difference in how the two clients ask. Compare
key scopes and roles first.

## Exit codes

One table for every command. Every code means the same thing whichever one
produced it, but not every code is reachable from every command — `doctor` owns
3, and nothing else ever reaches it.

| Code | Meaning |
|------|---------|
| **0** | `doctor`: every check passed, or only warned and `--strict` was not given. `status`: it completed. `watch`: the run completed — `--duration` elapsed. A failed job, a new signal and a failed delivery all still exit 0, because `watch` reaches no verdict. `scans list` / `drifts list`: every read arrived, including a run that legitimately found nothing. `scans jobs`: the history was read. `events list` / `events show` / every `plan` verb: the read arrived — including a page that stopped at `--limit` with more behind it, which is reported in the footer and in `truncated`, not in the exit code. `scans run` / `scans cancel` / `drifts dismiss` / `drifts reopen` / `annotate`: the API accepted the write — or `--dry-run` resolved everything and sent nothing. For `annotate` that includes a `200` for a label the API de-duplicated, which created nothing and is reported as such. `install`: the files are on disk and, unless `--no-start`, `pull` and `up -d` both succeeded and `/health` answered (or `--wait 0` skipped the wait). `upgrade`: the new tag is pinned and running — **or the pin already equalled `--to`**, which runs nothing and is deliberately 0 so a converging provisioning script is not a failing one. |
| **1** | The tool itself broke, or a command other than `doctor` could not complete a request — unreachable, or the API refused it (a project-scoped key with no `--project` gets a 403 here, on a perfectly healthy instance). `watch` reaches it two ways: a startup read it cannot proceed without (the project listing, or a project's scan listing), and a key revoked mid-run, which ends the run after a `watch.stopped` line carrying `reason: "authentication_failed"`. Every *other* failed read during a run is a `poll.degraded` line, not an exit. Three more routes into 1 belong to the object commands: **any** failed read in a `scans list` or `drifts list` fan-out, a `scans run` whose job came back already `failed`, and a `scans cancel`, `drifts dismiss` or `drifts reopen` you **declined at the prompt** — "the operator said no" must never be readable as "the mutation happened". `install` and `upgrade` reach 1 three ways of their own: `docker compose pull` or `up -d` exited non-zero, `/health` did not answer within `--wait`, or a file could not be written (a read-only directory, or a race with a second `tripl install`). The `events` and `plan` verbs reach 1 the ordinary way and only that way: each reads ONE resource of ONE project, so there is no partial answer to report beside a failure — a refused read is the client's message and exit 1, never an empty table at exit 0. **In none of those is anything rolled back** — for `install` the stack is started, and for a failed `up -d` the new pin is left in place on purpose. Declining the backup gate is also 1. **`doctor` should never exit 1** — it turns every API failure into a finding, so an exit 1 out of doctor is a bug report, not a diagnosis. |
| **2** | Usage or configuration error: a bad flag, an out-of-range value, no URL, no API key, an unreadable config file. For `doctor` and `status` that is always resolved before any socket opens. `watch` adds two refusals it can only reach *after* reading the project and scan listings — `--scan` matching nothing, and more than 24 selected scan configs — so for it the resolution is two rounds of HTTP in, not zero. The `scans` and `drifts` verbs add: a bare group with no verb, a missing or repeated `--project` on a command that acts on one object, a `<scan>` selector matching nothing or matching two configs, a `--snooze-until` that is not RFC 3339, a `--limit` outside 1–200, a `--status` that is not one of the six, and **`scans cancel` / `drifts dismiss` / `drifts reopen` on a non-TTY without `--yes`**. `annotate` adds: a `--url` that is not an absolute `http`/`https` URL or is over 500 characters, an `--at` that is not RFC 3339, a blank or over-long `<label>`, and `--scope-type` without `--scope-ref` or the reverse. The read groups add: a missing or repeated `--project` (every one of their routes is per project), a `--branch` or `<event-type>` selector matching nothing or matching two, a `--status` or `--type` outside the API's own enum, an `--offset`/`--limit` outside the route's range, a `<query>` that is blank or over 500 characters, and `--branch` on `plan branches`, which has no such flag. `install` and `upgrade` add: an explicit `--url` or `--api-key`, an `--app-url` that is not a URL, a `--version`/`--to` that is not a valid image tag, a `--wait` outside 0–3600, a `--dir` that looks like a tripl source checkout, no `docker` on `PATH` / no Compose v2 plugin / a daemon that will not answer, a `--dir` with no stack in it, a refused **downgrade**, an unorderable tag pair without `--yes`, and any prompt met on a non-TTY without `--yes`. Either way **no JSON is emitted**, no write is ever sent, and no file is written. |
| **3** | `doctor` only: at least one check failed — or, with `--strict`, at least one warned. No other command reaches 3, whatever it observes. |
| **130** | Interrupted (`Ctrl-C`). For `doctor` and `status` that is an abandoned run. For `watch` **it is the normal ending**: a run without `--duration` has no other way to stop, so 130 out of `watch` means "you pressed Ctrl-C", not "something went wrong". A wrapper that treats non-zero as failure needs to know this before it pages somebody. |

:::note Exit 1 out of a write is not always "it did not happen"
A declined prompt, a 403 and a 404 all exit 1 without changing anything. But so
does a `scans run` whose job **was** created and came back `failed`, and so does
a connection that dropped after the request left. When 1 comes out of a
mutation, read the message: it names which of the two you got. Re-run
`tripl scans jobs` before you retry.
:::

An unreachable instance therefore exits **3** out of `doctor`, not 1 — it is a
finding, like everything else doctor reads. That is precisely what makes an exit
1 out of `doctor` a meaningful signal. Every other command exits **1** on an
unreachable instance, because none of them turns a failed read into a verdict.

:::warning Credentials are required even for the connectivity check
Every command **that talks to an instance** demands both the URL and the API key
before it opens a connection, so a missing key is exit 2 — for `doctor` that
holds even though `/health` itself needs no key. That keeps "you have not
configured a key" cleanly apart from "the instance rejected your key" — two
failures that produced the same shrug during the incident.

`install` and `upgrade` are outside this rule in both directions: they need
neither value, and passing one explicitly is itself exit 2. Their `/health` poll
and their `/auth/status` read go out **unauthenticated**, with no `Authorization`
header, because at install time no account exists and therefore no key can.
:::

:::note Exit 1 out of `install` or `upgrade` never means "nothing happened"
It means the opposite of a declined write. By the time either reaches 1, the
files are on disk and — unless the failure was the `pull` — the containers have
been asked to start. A `/health` timeout in particular is often just a slow first
migration or a proxy that is not up yet. Read the message: it names which step
failed and which `docker compose logs` to open. Re-running is safe; that is what
idempotence is for.
:::

### In cron

```sh
#!/bin/sh
# /etc/cron.hourly/tripl-doctor - mails only when something is actually wrong.
set -u

export TRIPL_BASE_URL="https://tripl.example.com"
TRIPL_API_KEY="$(cat /etc/tripl/read-only-key)"
export TRIPL_API_KEY

report="$(tripl doctor --json 2>/dev/null)"
code=$?

case "$code" in
  0) exit 0 ;;                      # healthy, or warnings only: stay quiet
  3) ;;                             # checks failed: fall through and report
  2) echo "tripl doctor is misconfigured (exit 2)" >&2; exit 2 ;;
  *) echo "tripl doctor could not run (exit $code)" >&2; exit "$code" ;;
esac

# cron mails whatever lands on stdout.
printf '%s\n' "$report" | jq -r '
  .checks[].findings[]
  | select(.severity == "fail")
  | "[\(.code)] \(.project // "instance"): \(.message)"'
exit 1
```

### In CI

```yaml
- name: Check the tripl instance
  env:
    TRIPL_BASE_URL: https://tripl.example.com
    TRIPL_API_KEY: ${{ secrets.TRIPL_READ_ONLY_KEY }}
  run: uvx tripl doctor --strict
```

Use `--strict` in a gate you watch every run, and plain `doctor` in an
unattended cron: a job that goes red over a handful of untriaged schema drifts
gets muted, and then nobody sees the failing scan either.

`watch` belongs in neither. It reaches no verdict, so there is no exit code for
a gate to read, and it only ever saw what fell inside its polls. It is the
command you run *by hand*, next to the incident.

:::warning An unattended write needs `--yes` and a second key
`scans cancel` and `drifts dismiss` refuse to prompt when stdin is not a
terminal, so a cron job or a CI step must pass `--yes` — otherwise it exits 2
and sends nothing, every time. Such a job also needs a `tk_w_` key backed by an
editor or owner, which is **not** the read-only key the examples above export.
Do not promote the diagnostic cron's key to write scope so a second job can
share it; mint a second key, and treat an exit 2 from the writing job as a
configuration alarm rather than a flaky run.
:::

A deploy step that posts a chart marker is the one write that needs no `--yes`:
`annotate` never prompts. It still needs that second, `tk_w_` key — see
[`tripl annotate`](#in-a-github-actions-workflow) for a workflow step.

## `--json`

Every command accepts `--json`, and in every case the human-readable report
still happens — on **stderr**, so stdout carries machine-readable output and
nothing else. That includes the confirmation prompt, which is written to stderr
precisely so it can never land inside the document.

What lands on stdout differs by command, because a one-shot report and a follow
mode are not the same shape:

- **Every command except `watch`** puts **at most one JSON document, newline
  terminated, on stdout and nothing else** — exactly one whenever the command
  *completed*. `tripl doctor --json | jq` is a promise, not a habit: doctor
  turns every API failure into a finding, so it emits its document at exit 0 and
  at exit 3 alike. `scans list` and `drifts list` do the same at exit 1 when a
  read inside their *fan-out* failed — that is reported in the document, in
  `errors[]`. They emit nothing if the run never got that far, which for them
  means the project selection itself failed.
- **A command that could not complete writes NOTHING to stdout.** Every exit 2,
  and every exit 1 that comes from a request the API refused (403, 404, 409,
  422), an instance that could not be reached, a payload of the wrong shape, or
  a confirmation you declined. That covers **every documented failure of `scans
  jobs`, `scans run`, `scans cancel` and `drifts dismiss`**: they raise on the
  first refusal, so there is no document to emit and the reason is on stderr.
  `tripl scans run --json` against a read-only key prints zero bytes on stdout
  and exits 1. **Check the exit code before you parse**, and never treat empty
  stdout as a transient parse error worth retrying — a retried write is not the
  same thing as a retried read.
- **`install` and `upgrade`** follow the first rule with one useful difference:
  they emit their document at exit **1** as well as at exit 0, for the three
  failures that happen *after* the files are written — a failed `pull`, a failed
  `up -d`, and a `/health` timeout. Every exit 2 emits nothing, as everywhere
  else, and so does a file that could not be written, because at that point there
  is no plan outcome to report. The `exit_code` is *in* the document, so a
  provisioning script reads the reason out of the same object it already parsed.
- **`watch`** puts **JSON Lines**: **one object per event**, each on its own
  line, flushed the moment it is produced. There is no enclosing array, no
  trailing summary document, and no way to know in advance how many lines there
  will be — the stream ends when the run does. Per-line flushing is a
  correctness requirement rather than polish: a follow mode that block-buffers
  into `jq` shows nothing for minutes, which reads as a hang in the exact
  situation the command exists for. Use `jq -c` or any JSON Lines reader, never
  a whole-stdin parse.

### Stability contract

Within one `schema_version`, for every command:

- Key names are **never removed or retyped**.
- `status` / `severity` values, check `id`s, finding `code`s and `watch` event
  tokens are **never renamed or repurposed**.
- New keys, new check ids, new finding codes and new event tokens **may appear
  in any release**. Select by `id` — or, in `watch`, by `event` — never by array
  index and never by position in the stream.
- `title`, `summary` and `message` are **prose** and may be reworded at any
  time. `generated_at`, `duration_ms`, `requests` and `tool_version` vary per
  run.

**Assert on `code` and `evidence` — or, in `watch`, on `event` and `data`. Never
assert on prose.**

`schema_version` is **shared** by every command: it is one number for the whole
tool, so a consumer branches on `command` and never on a per-command version.
The `command` values are `"doctor"`, `"status"`, `"watch"`, `"scans list"`,
`"scans jobs"`, `"scans run"`, `"scans cancel"`, `"drifts list"`,
`"drifts dismiss"`, `"install"` and `"upgrade"` — the invocation with its space,
so `command` and what you typed are the same string.

Every document shares the same first six keys: `schema_version`, `tool`,
`tool_version`, `command`, `generated_at` and `duration_ms`.

Every document **of a command that talks to an instance** carries two more before
its own payload: `requests` (how many HTTP requests the run made) and `instance`
(the resolved base URL, where each value came from, and the key scope).

:::warning `install` and `upgrade` carry neither `requests` nor `instance`
Not as `null` — the keys are **absent**. There is no configured base URL and no
API key for a command that provisions a host, and an empty `instance` block would
let a consumer believe those fields were merely unset rather than inapplicable.
Branch on `command` before you read either key.
:::

Output is written ASCII-escaped, documents and lines alike: a non-ASCII
character inside a `message` reaches stdout as a `\uXXXX` sequence, which every
JSON parser (`jq` included) decodes back. The examples below show the decoded
form — only a raw `grep` over stdout would see the difference.

### `doctor` document

A complete run against a healthy instance:

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "doctor",
  "generated_at": "2026-07-31T19:12:51Z",
  "duration_ms": 42,
  "requests": 8,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "instance"
  },
  "status": "pass",
  "exit_code": 0,
  "summary": { "pass": 6, "warn": 0, "fail": 0, "skip": 0 },
  "checks": [
    {
      "id": "connectivity",
      "title": "Instance reachability",
      "status": "pass",
      "summary": "Reached https://tripl.example.com (from $TRIPL_BASE_URL); the API and its database are up.",
      "skip_reason": null,
      "findings": []
    },
    {
      "id": "auth",
      "title": "API key",
      "status": "pass",
      "summary": "The API key authenticates as an instance-wide key (role: owner).",
      "skip_reason": null,
      "findings": []
    },
    {
      "id": "projects",
      "title": "Projects",
      "status": "pass",
      "summary": "1 project selected.",
      "skip_reason": null,
      "findings": []
    },
    {
      "id": "data_sources",
      "title": "Data sources",
      "status": "pass",
      "summary": "1 data source(s) referenced by the selected scan configs; none reports a failed probe.",
      "skip_reason": null,
      "findings": []
    },
    {
      "id": "scans",
      "title": "Scheduled metrics collection",
      "status": "pass",
      "summary": "1 scheduled scan configs are collecting on time.",
      "skip_reason": null,
      "findings": []
    },
    {
      "id": "drifts",
      "title": "Schema drift",
      "status": "pass",
      "summary": "1 event type(s) examined; no untriaged schema drift.",
      "skip_reason": null,
      "findings": []
    }
  ]
}
```

Field notes:

- `instance.base_url_source` / `api_key_source` are verbatim provenance —
  `"--url"`, `"$TRIPL_BASE_URL"`, or the config file's path. *"Why is it talking
  to **that** instance"* was a week of the incident.
- `instance.api_key_scope` is `instance`, `project` or `unknown`. `status`
  always reports `unknown`: it never calls `/auth/me`.
- `summary` always carries all four keys, including the zeroes.
- `checks` always has exactly six entries, in the documented order.
- `skip_reason` is non-null **if and only if** `status == "skip"`.
- `exit_code` echoes what the shell saw, so a consumer reading only the document
  knows the verdict.

The `scans` check from the failing instance shown earlier — the complete object,
findings and all:

```json
{
  "id": "scans",
  "title": "Scheduled metrics collection",
  "status": "fail",
  "summary": "1 of 1 scheduled scan configs is not collecting.",
  "skip_reason": null,
  "findings": [
    {
      "code": "scan_config_failing",
      "severity": "fail",
      "project": "prod",
      "target": { "kind": "scan_config", "id": "scan-1", "name": "prod events" },
      "message": "Scan config 'prod events' (1h) has failed 5 consecutive scheduled runs since 2026-07-31T14:12:51Z. Last error: 'Scan failed due to an internal error.' - that is the backend's generic fallback, not the real cause, so the cause is in the worker log for job job-0.",
      "evidence": {
        "consecutive_failures": 5,
        "last_error": "Scan failed due to an internal error.",
        "last_error_is_generic": true,
        "last_failed_job_id": "job-0",
        "last_failed_at": "2026-07-31T18:12:51Z",
        "last_success_at": "2026-07-31T11:12:51Z",
        "interval": "1h",
        "data_source_id": "ds-1"
      }
    },
    {
      "code": "scan_backoff_active",
      "severity": "warn",
      "project": "prod",
      "target": { "kind": "scan_config", "id": "scan-1", "name": "prod events" },
      "message": "The scheduler has deliberately deferred the next attempt to not before 2026-07-31T22:12:51Z (about 4h after the last failure): 3 or more consecutive failures trigger a backoff, so the worker is not stuck.",
      "evidence": {
        "consecutive_failures": 5,
        "backoff_after": 3,
        "interval_seconds": 3600,
        "deferred_by_seconds_estimate": 14400,
        "next_attempt_not_before_estimate": "2026-07-31T22:12:51Z"
      }
    }
  ]
}
```

Every finding has the same shape: `code`, `severity` (`warn` or `fail` — never
`pass` or `skip`), `project` (may be `null`), `target` (may be `null`), `message`
and `evidence`.

### Evidence keys, by finding code

| Check | Code | Severity | `evidence` keys |
|-------|------|----------|-----------------|
| `connectivity` | `instance_unreachable` | fail | `url`, `status_code`, `body_status` |
| `connectivity` | `instance_database_unreachable` | fail | `url`, `status_code`, `body_status` |
| `connectivity` | `instance_not_tripl` | fail | `url`, `status_code`, `body_status` |
| `auth` | `api_key_invalid` | fail | `status_code`, `detail` |
| `auth` | `auth_unexpected_status` | fail | `status_code`, `detail` |
| `projects` | `project_selector_required` | fail | `api_key_scope` |
| `projects` | `project_list_forbidden` | fail | `status_code`, `error` |
| `projects` | `project_not_found` | fail | `slug`, `status_code` |
| `projects` | `project_not_authorized` | fail | `slug`, `status_code` |
| `projects` | `no_projects_selected` | warn | `excluded_demo_count` |
| `projects` | `project_generation_failed` | fail | `generation_status`, `generation_stage`, `generation_error` |
| `projects` | `project_generating` | warn | `generation_status`, `generation_stage` |
| `data_sources` | `data_source_probe_failed` | fail | `data_source_id`, `data_source_name`, `last_test_status`, `last_test_at`, `last_test_message`, `message_redacted`, `scan_config_ids` |
| `data_sources` | `data_source_probe_stale` | warn | the same, plus `streak_started_at` |
| `scans` | `scan_config_not_dispatchable` | fail | `interval`, `time_column` |
| `scans` | `scan_config_failing` | fail | `consecutive_failures`, `consecutive_failures_truncated`, `jobs_window`, `last_error`, `last_error_is_generic`, `last_failed_job_id`, `last_failed_at`, `last_success_at`, `interval`, `data_source_id` |
| `scans` | `scan_history_window_full` | warn | `interval`, `jobs_window` |
| `scans` | `scan_backoff_active` | warn | `consecutive_failures`, `backoff_after`, `interval_seconds`, `deferred_by_seconds_estimate`, `next_attempt_not_before_estimate` |
| `scans` | `scan_watermark_stale` | warn | `interval`, `time_to`, `behind_seconds` |
| `scans` | `scan_never_collected` | fail | `interval`, `created_at`, `age_seconds`, `jobs_seen` |
| `scans` | `scan_not_dispatched` | fail | `interval`, `last_dispatched_at`, `idle_seconds` |
| `scans` | `scan_interval_unknown` | warn | `interval` |
| `drifts` | `schema_field_deleted_by_accept` | warn | `field_name`, `event_type_id`, `drift_id`, `resolved_at`, `resolved_by` |
| `drifts` | `schema_drift_open` | warn | `untriaged_count`, `oldest_detected_at`, `examples` |
| `drifts` | `drift_scan_truncated` | warn | `project`, `examined`, `total` |
| *several* | `endpoint_unexpected_status` | fail | `path`, `status_code`, `error` |

All timestamps are RFC 3339 in UTC, second precision, with a literal `Z`.

### `status` document

Same envelope, with `command: "status"`, `instance.api_key_scope` always
`"unknown"`, and a `projects` array in place of `status` / `exit_code` /
`summary` / `checks`:

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "status",
  "generated_at": "2026-07-31T19:08:09Z",
  "duration_ms": 15,
  "requests": 2,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "unknown"
  },
  "projects": [
    {
      "slug": "mobile",
      "name": "Mobile",
      "is_demo": false,
      "events": { "total": 98, "active": 95, "event_types": 6 },
      "scans": { "total": 2, "failing": 1 },
      "signals": { "significant_open": 3 },
      "monitors": { "firing": 1 },
      "coverage": { "days": 7, "pct": 91.4, "matched": 388, "total": 425 },
      "errors": []
    }
  ]
}
```

`coverage` is `null` when the coverage read failed, and the reason appears in
that project's `errors` array as `{"section", "status_code", "message"}`. A
failed section never blanks the row — the counts that did arrive are still the
answer.

### `watch` lines

`watch --json` shares the first four keys with the documents above and then
diverges. There is no `generated_at` / `duration_ms` / `requests` block, because
those three describe a run that has already ended, and `instance` moves down into
`watch.started`'s `data`. **Every line carries every key**, without exception —
a consumer never has to test for presence at the top level.

| Key | Type | Meaning |
|-----|------|---------|
| `schema_version` | int | The same number the `doctor` and `status` documents carry. |
| `tool` | string | Always `"tripl"`. |
| `tool_version` | string | The CLI version. Varies per release. |
| `command` | string | Always `"watch"`. |
| `stream` | string | `"meta"`, `"event"` or `"diagnostic"` — see below. Derived from `event`, so the two can never disagree. |
| `seq` | int | Monotonic from 1 within one run, never reused. A gap means the stream was truncated, and sorting by it undoes whatever a log shipper did to the order. |
| `time` | string | When `watch` **observed** the change — RFC 3339, UTC, second precision, literal `Z`. Not when it happened: every domain timestamp is a separately named field inside `data`. |
| `event` | string | The token. This is the contract — see [Event tokens](#event-tokens) for the vocabulary. |
| `project` | string or null | Project slug. `null` on the `meta` lines, which are about the run rather than about a project. |
| `target` | object or null | `{"kind", "id", "name"}`, the same shape a `doctor` finding uses. `null` when the line is not about one object. |
| `message` | string | **Prose**: the human line without its timestamp and token. May be reworded in any release. |
| `data` | object | The payload, under the **backend's own field names**. A key the API did not return at all is **absent**; a key it returned as null is `null`. The distinction is deliberate — a `metrics_collection` job has no replay counters and `watch` does not invent them, so `replay_chunks_total` is missing rather than 0. |

`stream` exists so that a consumer can count the right things. Three values, and
the split is load-bearing: an incident count must not include the CLI's own
transport trouble, and "how much of the window was `watch` blind for" has to
stay answerable.

| `stream` | Tokens | What it is |
|----------|--------|------------|
| `meta` | `watch.started`, `watch.stopped` | The run's own frame: what was followed, and how it ended. |
| `event` | every `job.*`, `signal.*` and `delivery.*` token | What happened on the instance. |
| `diagnostic` | `poll.degraded`, `poll.recovered` | What `watch` could not see. Never an instance event. |

One real line of each class, pretty-printed here; on the wire each is a single
line.

**`meta`** — the first line of every run, and the one that records what the run
was actually configured to do:

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "watch",
  "stream": "meta",
  "seq": 1,
  "time": "2026-07-31T19:10:41Z",
  "event": "watch.started",
  "project": null,
  "target": null,
  "message": "1 project, 1 scan config, poll 10s.",
  "data": {
    "instance": {
      "base_url": "https://tripl.example.com",
      "base_url_source": "$TRIPL_BASE_URL",
      "api_key_source": "$TRIPL_API_KEY",
      "api_key_scope": "unknown"
    },
    "projects": ["prod"],
    "interval_seconds": 10.0,
    "duration_seconds": null,
    "stall_after_seconds": 120.0,
    "jobs_limit": 10,
    "deliveries_limit": 20,
    "slow_stream_min_seconds": 30.0,
    "requests_per_fast_tick": 1,
    "requests_per_slow_tick": 4,
    "baseline": {
      "running_jobs": 1,
      "pending_jobs": 0,
      "open_signals": 0,
      "significant_open_signals": 0,
      "failed_deliveries": 0
    }
  }
}
```

`api_key_scope` is `"unknown"` here for the same reason it is in the `status`
document: `watch` does not read `/auth/me`, and saying "unknown" is cheaper and
more honest than a guess. `baseline` is the preamble's counts — the state that
was already true at attach time and is therefore **never re-emitted as events**.

The last line of every run is `watch.stopped`, also `meta`. Its `data` carries
`reason` (`"interrupted"`, `"duration_elapsed"` or `"authentication_failed"`),
`elapsed_seconds`, `ticks`, `requests`, and `counts` — a tally of every token
emitted during the run, which never counts the stopped line itself.

**`event`** — the line the incident needed, a replay advancing one chunk:

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "watch",
  "stream": "event",
  "seq": 2,
  "time": "2026-07-31T19:10:51Z",
  "event": "job.progress",
  "project": "prod",
  "target": { "kind": "scan_job", "id": "job-91c2", "name": "nightly replay" },
  "message": "'nightly replay' job job-91c2 chunk 4 of 18 (22.2%) collecting 2026-07-05T00:00:00Z..2026-07-06T00:00:00Z, 2m elapsed.",
  "data": {
    "scan_config_id": "scan-1",
    "scan_name": "nightly replay",
    "status": "running",
    "error_message": null,
    "mode": "metrics_replay",
    "replay_progress_phase": "collecting",
    "replay_chunks_total": 18,
    "replay_chunks_completed": 4,
    "replay_progress_percent": 22.2,
    "replay_current_chunk_index": 4,
    "replay_current_chunk_from": "2026-07-05T00:00:00+00:00",
    "replay_current_chunk_to": "2026-07-06T00:00:00+00:00",
    "replay_chunk_interval": "1d",
    "time_from": "2026-07-01T00:00:00+00:00",
    "time_to": "2026-07-31T00:00:00+00:00",
    "created_at": "2026-07-31T19:08:41Z",
    "started_at": "2026-07-31T19:08:41Z",
    "completed_at": null,
    "updated_at": "2026-07-31T19:10:51Z",
    "elapsed_seconds": 130
  }
}
```

`replay_chunks_completed`, `replay_progress_phase` and the rest are the worker's
own key names, unrenamed on purpose: the same string is greppable in the
backend's replay task and in this output, so there is no second spelling to
drift. Two fields are `watch`'s own rather than the job response's:
`elapsed_seconds`, the age of the job at `time`, and `scan_name`, resolved from
the scan listing so a consumer does not need a second lookup to name the config.
`target.name` carries the same name.

:::note Timestamps inside `data` are verbatim, and not all of them match `time`
The envelope's `time` is normalized by the CLI. Everything inside `data` is
copied **exactly as the API returned it** and is never reformatted, which is why
the job's own `started_at` ends in `Z` (it comes from the response model) while
the replay block's `replay_current_chunk_from` ends in `+00:00` (it comes from
the worker's `result_summary`). Parse these values; do not string-compare them,
and do not assume one spelling.
:::

**`diagnostic`** — a read that did not come back. This line is why a non-200 can
never be mistaken for an empty result:

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "watch",
  "stream": "diagnostic",
  "seq": 7,
  "time": "2026-07-31T19:10:51Z",
  "event": "poll.degraded",
  "project": "prod",
  "target": null,
  "message": "signals read failed: HTTP 500 on /projects/{slug}/anomalies/signals. Signal lines are suspended until it recovers - no signal lines does NOT mean no signals.",
  "data": {
    "section": "signals",
    "path": "/projects/{slug}/anomalies/signals",
    "status_code": 500,
    "error": "HTTP 500",
    "consecutive_failures": 1,
    "target": "prod"
  }
}
```

`section`, `path`, `status_code` and `error` are deliberately the key names
`doctor` uses for `endpoint_unexpected_status`, so an extractor written for one
works on the other unchanged. Note that `data.target` on this line is the
**stream's** reference — a project slug, or `slug/scan_config_id` for the `jobs`
section — and is unrelated to the envelope's `target`, which stays `null` on
every diagnostic line.

`poll.degraded` has a second shape, for a full window in which every row was
new: the same keys, with `status_code` and `error` `null` and
`consecutive_failures` `0`, plus `"window_full": true` and `"window"` holding
the limit that was hit. Nothing failed in that case — older rows may simply have
been pushed out between polls.

`poll.recovered` closes a `poll.degraded` run, and its `data` is
`section`, `path`, `failed_polls`, `gap_seconds` and `events_during_gap` — the
last being the number that tells you whether the quiet stretch was `watch` being
blind or the instance being calm.

### `scans list` document

The shared envelope plus a `projects` array. Each project carries `slug`,
`name`, `is_demo`, `scans` and `errors`, and **all five are always present** —
`scans` is `[]` when the listing failed, and the `errors` entry beside it is
what says so.

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "scans list",
  "generated_at": "2026-07-31T19:12:51Z",
  "duration_ms": 31,
  "requests": 3,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "unknown"
  },
  "projects": [
    {
      "slug": "prod",
      "name": "Prod",
      "is_demo": false,
      "scans": [
        {
          "id": "scan-1",
          "name": "prod events",
          "interval": "1h",
          "time_column": "event_time",
          "data_source_id": "ds-1",
          "event_type_id": "et-1",
          "event_type_column": "event_name",
          "event_name_format": "snake_case",
          "replay_chunk_interval": "1d",
          "scan_lookback_hours": 24,
          "created_at": "2026-06-01T09:14:00Z",
          "updated_at": "2026-07-30T11:02:00Z",
          "dispatchable": true
        }
      ],
      "errors": []
    },
    {
      "slug": "mobile",
      "name": "Mobile",
      "is_demo": false,
      "scans": [],
      "errors": [
        {
          "section": "scans",
          "endpoint": "/projects/mobile/scans",
          "status_code": 403,
          "message": "Forbidden (403): the API key lacks the required scope (tk_r_ keys cannot write), is scoped to a different project, or the backing user role is insufficient. API detail: Not authorized for project"
        }
      ]
    }
  ]
}
```

Those twelve keys plus `dispatchable` are **the whole projection** — the same
one the MCP `list_scans` tool returns. A key the API did not return is absent
rather than null, so test for presence before reading `scan_lookback_hours` on
an old instance. `dispatchable` is the CLI's own field, not the API's: it is
`interval` and `time_column` both being set, which is the dispatcher's own
selection predicate.

`errors[]` entries are `{section, endpoint, status_code, message}`, and
`endpoint` is the **concrete** path that failed — not its template — because an
operator listing six projects cannot act on a message that does not say which
one. `status_code` is `null` when nothing answered at all.

### `scans jobs` document

`project`, `scan` (`{id, name}`), the `limit` that was **requested**, and `jobs`
— `ScanJobResponse` rows **verbatim, newest first**. Nothing is trimmed here:
`result_summary` carries the replay chunk counters `watch` reads, and
`error_message` is the whole answer to *why did it fail*.

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "scans jobs",
  "generated_at": "2026-07-31T19:12:51Z",
  "duration_ms": 31,
  "requests": 2,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "unknown"
  },
  "project": "prod",
  "scan": { "id": "scan-1", "name": "prod events" },
  "limit": 3,
  "jobs": [
    {
      "id": "job-91c2",
      "status": "running",
      "created_at": "2026-07-31T19:08:41Z",
      "completed_at": null,
      "error_message": null
    }
  ]
}
```

`limit` is what was asked for. Compare it against `jobs | length` before
concluding a config has no older history.

### `drifts list` document

`status_filter` echoes the `--status` that produced the list, and each project
reports its **own** budget spend:

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "drifts list",
  "generated_at": "2026-07-31T19:12:51Z",
  "duration_ms": 31,
  "requests": 3,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "unknown"
  },
  "status_filter": "all",
  "projects": [
    {
      "slug": "prod",
      "name": "Prod",
      "is_demo": false,
      "event_types_total": 17,
      "event_types_examined": 17,
      "truncated": false,
      "drifts": [
        {
          "id": "drift-1",
          "field_name": "cart_value",
          "drift_type": "type_changed",
          "status": "open",
          "detected_at": "2026-07-28T04:10:00Z",
          "event_type_name": "app.screen_view",
          "untriaged": true
        }
      ],
      "errors": [
        {
          "section": "drifts",
          "endpoint": "/projects/prod/event-types/et-9/drifts",
          "status_code": 404,
          "message": "Not found (404): Event type not found"
        }
      ]
    },
    {
      "slug": "mobile",
      "name": "Mobile",
      "is_demo": false,
      "event_types_total": 240,
      "event_types_examined": 183,
      "truncated": true,
      "drifts": [],
      "errors": []
    }
  ]
}
```

A drift row is the API's `SchemaDriftResponse` **verbatim** — all fourteen
fields, because `resolved_by` and `resolution_note` are how an accidental field
deletion gets traced — plus the two facts the API cannot carry:
`event_type_name`, resolved from the event-type listing so a consumer needs no
second lookup, and `untriaged`, this run's evaluation of *open, or snoozed past
its snooze*.

`event_types_examined` and `truncated` are **per project**, deliberately: the
budget is spent round-robin, so one instance-wide ratio would name no project,
and *"we did not look there"* is only useful when it says where.

:::warning `"drifts": []` is not "no drifts"
Not on its own. It means "no drifts **matching `status_filter`** among the
`event_types_examined` event types whose reads succeeded". Check `errors`,
compare `event_types_examined` with `event_types_total`, and check the process
exit code. The human output prints all three on one screen for exactly this
reason.
:::

### `events` and `plan` documents

The seven read verbs share **one document shape**. `items` holds the rows
whichever verb produced them — including `events show`, which puts its single
event there rather than inventing a second shape for the one command that
returns exactly one thing. `jq '.items[]'` therefore works across the whole
group.

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "events list",
  "generated_at": "2026-08-01T09:12:51Z",
  "duration_ms": 24,
  "requests": 1,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "unknown"
  },
  "project": "prod",
  "branch": null,
  "kind": "event",
  "total": 412,
  "offset": 0,
  "limit": 2,
  "truncated": true,
  "meta": {},
  "items": [
    {
      "id": "evt-1",
      "name": "app.screen_view.viewed",
      "status": "live",
      "event_type_id": "et-1",
      "last_seen_at": "2026-07-31T19:00:00Z",
      "drift_count": 2,
      "tags": [{ "id": "tag-checkout", "name": "checkout" }],
      "field_values": [
        { "id": "fv-1", "field_definition_id": "fd-1", "value": "checkout" }
      ],
      "meta_values": []
    }
  ]
}
```

Every key above is present on all seven, so a consumer never tests for one
before reading it.

| Key | Meaning |
|-----|---------|
| `project` | The slug that was read. Always exactly one. |
| `branch` | `{id, name}` when `--branch` was passed, `null` when it was not. `null` means "no branch was named", not "this is main": `--branch main` reads the live plan and still carries main's own id and name, so branch on whether the flag was given rather than on `null` — see [`--branch`](#the---branch-flag). |
| `kind` | What one member of `items` **is**: `event`, `event_type`, `field`, `variable`, `branch` or `search_result`. Branch on this, never on `command`'s wording. |
| `total` | The count the API reported **before** paging, or `null` where the route reports none. |
| `offset` / `limit` | What was requested, or `null` where the route takes no such parameter. `plan search` has no offset; `plan types`, `plan fields` and `plan branches` page nothing at all. |
| `truncated` | Whether rows were left behind. The route's own answer where it reports one — `plan search` does, and it is exact; otherwise whether `total` exceeds `offset` plus the rows returned; and where a route reports neither, whether the page filled. Derived here so nobody has to do that arithmetic twice. |
| `meta` | Facts the **route** reported about the answer rather than about a row. `{}` on six of the seven; `{"semantic_used": bool}` on `plan search`. |
| `items` | The API's own objects, **verbatim**. |

:::warning `"truncated": true` is not an error, and `"items": []` is not "none exist"
Truncation is rows left behind: a full page with more behind it, or a route that
says outright it dropped ranked hits. The exit code stays 0, because nothing
failed — the CLI asked for a page and got one. A loop that reads
`items` and stops without checking `truncated` will silently process the first
`limit` rows of a catalog forever.

`"items": []` means "nothing matched **these filters** on **this branch**". A
failed read never reaches this document at all: these verbs read one resource of
one project, so a refusal is exit 1 with the client's message and no JSON.
:::

:::note Rows are verbatim, and that is a decision
`scans list` trims its rows because `base_query` is kilobytes of free-text SQL
that answers no operational question. Nothing here is trimmed, for the opposite
reason: a CLI writes to a pipe, where a dropped field is one you have to go and
fetch again with a second command.

The MCP server does trim the same payloads — `list_events` returns nine keys per
event, not thirty. That is a statement about a *model's* context budget, where
every token is paid for on every turn, and it stays with that consumer. If these
documents ever start trimming, it is a contract change and it will be announced
as one.
:::

### Mutation documents

`scans run`, `scans cancel`, `drifts dismiss`, `drifts reopen` and `annotate`
share one shape. **Every key is present on every one of them**, `null` where it does not apply, so a consumer
never has to test for existence before reading — the same rule the `doctor`
summary follows.

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "drifts dismiss",
  "generated_at": "2026-07-31T19:12:51Z",
  "duration_ms": 31,
  "requests": 1,
  "instance": {
    "base_url": "https://tripl.example.com",
    "base_url_source": "$TRIPL_BASE_URL",
    "api_key_source": "$TRIPL_API_KEY",
    "api_key_scope": "unknown"
  },
  "dry_run": false,
  "request": {
    "method": "POST",
    "path": "/projects/prod/event-types/drifts/drift-1/actions",
    "params": {},
    "body": {
      "action": "snooze",
      "note": "waiting on the mobile release",
      "snoozed_until": "2026-08-04T00:00:00Z"
    }
  },
  "project": "prod",
  "scan": null,
  "job_id": null,
  "drift_id": "drift-1",
  "action": "snooze",
  "deduplicated": null,
  "result": {
    "id": "drift-1",
    "field_name": "cart_value",
    "drift_type": "type_changed",
    "status": "snoozed"
  }
}
```

| Key | Meaning |
|-----|---------|
| `dry_run` | `true` when nothing was sent. |
| `request` | What **was** sent, or under `--dry-run` what **would** be. Exactly `method`, `path`, `params`, `body` — never headers, never the API key. `params` drops nulls, so it is what would go on the wire. |
| `project` | The single `--project` slug. |
| `scan` | `{id, name}` for the two `scans` verbs, else `null`. |
| `job_id` | `scans cancel` only, else `null`. |
| `drift_id` | `drifts dismiss` and `drifts reopen` only, else `null`. |
| `action` | `"false_positive"` or `"snooze"` for `drifts dismiss`, else `null`. |
| `deduplicated` | `annotate` only: `false` when the API created the annotation (`201`), `true` when it returned one that already existed (`200`). `null` for every other verb and under `--dry-run`. |
| `result` | The API's response object, verbatim. **`null` under `--dry-run`, always** — nothing was sent, so there is no result, and the absence is the statement. |

For `scans run` the same document carries `"command": "scans run"`,
`"scan": {"id": ..., "name": ...}`, `"action": null`, and `result` is the
created `ScanJobResponse`. **Read `result.status` before you call it a
success**: a `201` with `"status": "failed"` is what a broker outage looks like,
and it is the reason that case exits 1.

For `annotate` it carries `"command": "annotate"`, `scan`, `job_id`, `drift_id`
and `action` all `null`, `request.body` with `"source": "api"`, and `result` is
the `ChartAnnotationResponse`. Read `deduplicated` before you report "annotation
created": a retried job gets `true`, and the `result` it holds is the earlier
annotation.

### `install` document

A completed first run. Note the two absent keys — no `requests`, no `instance`.

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "install",
  "generated_at": "2026-08-01T09:00:00Z",
  "duration_ms": 74210,
  "directory": "/srv/tripl",
  "app_base_url": "https://tripl.example.com",
  "image": "ghcr.io/vladenisov/tripl",
  "version": "1.5.0",
  "dry_run": false,
  "files": [
    {
      "path": "compose.yaml",
      "action": "create",
      "mode": "0644",
      "note": "",
      "keys": []
    },
    {
      "path": "infra/rabbitmq/rabbitmq.conf",
      "action": "create",
      "mode": "0644",
      "note": "",
      "keys": []
    },
    {
      "path": ".env",
      "action": "create",
      "mode": "0600",
      "note": "",
      "keys": [
        "APP_BASE_URL",
        "TRIPL_IMAGE",
        "TRIPL_VERSION",
        "ENCRYPTION_KEY",
        "SECRET_KEY",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASSWORD"
      ]
    }
  ],
  "secrets_generated": [
    "ENCRYPTION_KEY",
    "SECRET_KEY",
    "POSTGRES_PASSWORD",
    "RABBITMQ_PASSWORD"
  ],
  "commands": [
    {
      "argv": ["docker", "compose", "pull"],
      "cwd": "/srv/tripl",
      "env": {},
      "returncode": 0
    },
    {
      "argv": ["docker", "compose", "up", "-d"],
      "cwd": "/srv/tripl",
      "env": {},
      "returncode": 0
    }
  ],
  "health": {
    "status": "ok",
    "waited_seconds": 68.4,
    "attempts": 14,
    "last_error": null
  },
  "bootstrap": {
    "has_users": false,
    "registration_enabled": true
  },
  "exit_code": 0
}
```

| Key | Meaning |
|-----|---------|
| `directory` | Always absolute, whatever `--dir` you typed. |
| `app_base_url` | The normalised `--app-url`. Also the origin `health` was polled at. |
| `image` / `version` | What was pinned in `.env`. |
| `dry_run` | `true` when nothing was written and nothing was run. |
| `files[]` | One entry per planned file, in the order they are applied. `path` is relative to `directory`; `action` is one of `create`, `replace`, `append`, `unchanged`, `kept`; `mode` is the mode **asked for**, as a string (`"0600"` — an integer `384` is not a file mode anybody reads); `note` explains a `kept` (`"differs from the version this CLI ships"`, or `"complete"` for a `.env` that already had every key); `keys` holds variable **names** and never values. |
| `secrets_generated` | The **names** of the secrets this run produced. Empty on a re-run that left `.env` alone. There is a test asserting no generated value appears anywhere in this document. |
| `commands[]` | Every **planned** command with `argv`, `cwd`, the env overlay, and `returncode`. A command that was never reached carries `"returncode": null` rather than being omitted — "the pull failed so `up -d` never ran" is exactly the fact you need, and an absent entry would read as "it ran and we lost the code". Empty under `--no-start`. Compose's own output is not here by construction; see [above](#what-actually-runs-and-where-its-output-goes). |
| `health` | `null` when the run never got there (including `--dry-run`). Otherwise `status` is `ok`, `timeout` or `skipped`; `last_error` carries the last probe's own message (`"HTTP 502"`, `"ConnectError"`) and is `null` when it succeeded or was skipped. |
| `bootstrap` | The `/auth/status` body verbatim — `has_users`, `registration_enabled` — or `null` if it could not be read. That failure never changes the exit code; it only changes the wording of the next steps. |
| `exit_code` | The process exit code, in the document. |

### `upgrade` document

```json
{
  "schema_version": 1,
  "tool": "tripl",
  "tool_version": "0.1.0",
  "command": "upgrade",
  "generated_at": "2026-08-01T09:00:00Z",
  "duration_ms": 51880,
  "directory": "/srv/tripl",
  "current_version": "1.4.0",
  "target_version": "1.5.0",
  "ordering": "upgrade",
  "dry_run": false,
  "backup_command": "backup   cd /srv/tripl && docker compose exec -T postgres \\\n           pg_dump -U tripl tripl | gzip > tripl-1.4.0.sql.gz\n         Take it now. This applies Alembic migrations, which are not reversible.\n         ENCRYPTION_KEY lives in .env, not in that dump - back it up separately.",
  "env_backup": ".env.bak.20260801T090000Z",
  "commands": [
    {
      "argv": ["docker", "compose", "pull"],
      "cwd": "/srv/tripl",
      "env": {"TRIPL_VERSION": "1.5.0"},
      "returncode": 0
    },
    {
      "argv": ["docker", "compose", "up", "-d"],
      "cwd": "/srv/tripl",
      "env": {},
      "returncode": 0
    }
  ],
  "health": {
    "status": "ok",
    "waited_seconds": 47.1,
    "attempts": 10,
    "last_error": null
  },
  "exit_code": 0
}
```

`commands`, `health`, `dry_run`, `directory` and `exit_code` mean exactly what
they mean for `install`. The rest:

| Key | Meaning |
|-----|---------|
| `current_version` | The `TRIPL_VERSION` read out of `.env` before anything ran. |
| `target_version` | Your `--to`. |
| `ordering` | `same`, `upgrade`, `downgrade` or `unknown` — the comparison [above](#how-the-two-tags-are-compared). A `downgrade` never reaches a document; it is exit 2. |
| `backup_command` | The multi-line `pg_dump` block that was printed, verbatim, so a wrapper can show or log the same text. It was **not** run. |
| `env_backup` | The **basename** of the `.env` copy taken before the pin was moved, or `null` if `.env` was never rewritten. Non-null is the fact a rollback needs; it sits next to `.env` in `directory`. |

Note the env overlay on the pull and not on the `up`: that asymmetry is the whole
[ordering rule](#order-of-operations-and-why), and it is visible in the document.

Useful one-liners:

```bash
# Just the failing check ids.
tripl doctor --json | jq -r '.checks[] | select(.status=="fail") | .id'

# Every scan config that is failing, with its last error.
tripl doctor --json \
  | jq -r '.checks[] | select(.id=="scans") | .findings[]
           | select(.code=="scan_config_failing")
           | "\(.target.name)\t\(.evidence.consecutive_failures)\t\(.evidence.last_error)"'

# Projects with open significant signals.
tripl status --json | jq -r '.projects[] | select(.signals.significant_open > 0) | .slug'

# Live chunk progress of every running replay, one line per advance.
# jq reads JSON Lines by default; --unbuffered is what keeps it live.
tripl watch --json \
  | jq --unbuffered -r 'select(.event=="job.progress")
         | "\(.time)\t\(.data.scan_name)\t\(.data.replay_chunks_completed)/\(.data.replay_chunks_total)"'

# The instance events only, with the CLI's own transport trouble excluded.
tripl watch --json | jq -c --unbuffered 'select(.stream=="event")'

# How blind was the run: every gap, with the events that came out of it.
tripl watch --json --duration 3600 \
  | jq -r 'select(.event=="poll.recovered")
           | "\(.data.section)\t\(.data.gap_seconds)s\t\(.data.events_during_gap) events"'

# Every scan config the scheduler will never select, across every project.
tripl scans list --json \
  | jq -r '.projects[] as $p | $p.scans[]
           | select(.dispatchable == false)
           | "\($p.slug)\t\(.name)\t\(.interval // "no interval")\t\(.time_column // "no time column")"'

# Did any project's scan listing fail? Never read the table without this.
tripl scans list --json | jq -r '.projects[] | select(.errors | length > 0) | .slug'

# The newest failed job of one config, with its error.
tripl scans jobs 'prod events' --project prod --json \
  | jq -r 'first(.jobs[] | select(.status=="failed"))
           | "\(.id)\t\(.completed_at)\t\(.error_message)"'

# Untriaged drifts, oldest first, as slug/event-type/field.
tripl drifts list --json \
  | jq -r '.projects[] as $p | $p.drifts[] | select(.untriaged)
           | [.detected_at, $p.slug, .event_type_name, .field_name, .drift_type] | @tsv' \
  | sort

# Projects the drift budget could not finish.
tripl drifts list --json \
  | jq -r '.projects[] | select(.truncated)
           | "\(.slug): \(.event_types_examined) of \(.event_types_total)"'

# What a write WOULD send, as real JSON rather than the human line's repr.
tripl drifts dismiss drift-1 --project prod --dry-run --json | jq '.request'

# Which files an install would touch, before it touches them.
tripl install --app-url https://tripl.example.com --dry-run --json \
  | jq -r '.files[] | "\(.action)\t\(.mode)\t\(.path)"'

# Did the stack come up, and how long did it take to answer?
tripl install --app-url https://tripl.example.com --yes --json \
  | jq -r '"\(.health.status) after \(.health.waited_seconds)s in \(.health.attempts) attempts"'

# Which compose command failed, if one did. null means it was never reached.
tripl upgrade --to 1.5.0 --yes --json \
  | jq -r '.commands[] | select(.returncode != 0) | "\(.argv|join(" ")) -> \(.returncode)"'

# The .env backup to restore from, empty if the pin was never moved.
tripl upgrade --to 1.5.0 --yes --json | jq -r '.env_backup // empty'

# Events your plan calls live that nothing has sent for a month.
tripl events list --project prod --status live --silent-since-days 30 --json \
  | jq -r '.items[] | .name'

# Did that page carry the whole catalog? Never read `.items` without this.
tripl events list --project prod --json | jq '{total, truncated}'

# One event's field values, under the names they set.
tripl events show evt-1 --project prod --json \
  | jq -r '.items[0].field_values[] | "\(.field_definition_id)=\(.value)"'

# Every required field of every event type, as `<type>.<field>`.
tripl plan types --project prod --json | jq -r '.items[].name' \
  | while read -r type; do
      tripl plan fields "$type" --project prod --json \
        | jq -r --arg t "$type" '.items[] | select(.is_required) | "\($t).\(.name)"'
    done

# Variables carrying values outside their documented set.
tripl plan variables --project prod --json \
  | jq -r '.items[] | select(.open_drift_count > 0) | "\(.name) \(.open_drift_count)"'

# Working branches whose base has moved under them - rebase before review.
tripl plan branches --project prod --json \
  | jq -r '.items[] | select(.behind_base) | .name'

# Did the semantic index answer, or did you get substring matches?
tripl plan search 'checkout funnel' --project prod --json | jq '.meta.semantic_used'
```

## See also

- [Self-hosting & Deployment](./deployment.md) — the reference behind
  [`tripl install`](#tripl-install): what each service is, how CORS and TLS have
  to be arranged around it, and the by-hand path for a host that cannot run this
  CLI.
- [Configuration reference](./configuration.md) — every environment variable, of
  which the generated `.env` sets seven.
- [Operations Runbook](./runbook.md) — health checks, backups, scaling, rollback.
- [Troubleshooting](../use/troubleshooting.md) — symptom-driven debugging for the
  problems doctor names.
- [MCP server](../integrate/mcp-server.md) — the other first-party client of the
  same API, configured with the same `TRIPL_BASE_URL` / `TRIPL_API_KEY` and
  built on the [same request layer](#one-request-layer-shared-with-the-mcp-server).
  Its `list_scans` / `trigger_scan` / `get_scan_status` tools are the agent-side
  spelling of `tripl scans`.
- [Agent API guide](../integrate/agent-api-guide.md) — the REST surface both
  clients speak.
- [API keys & governance](../administer/admin-guide.md#api-keys--governance) —
  scopes, project binding, expiry.
