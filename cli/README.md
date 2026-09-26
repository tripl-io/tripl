# tripl (CLI)

Operator CLI for a **running tripl instance** — diagnostics, health and live
monitoring. Like [`tripl-mcp`](../mcp-server), it is a pure HTTP client of the
tripl REST API (`/api/v1`): it imports no backend code and never touches the
database.

The distribution is **`tripl`**, the console script is **`tripl`**, and the
import package is **`tripl_cli`**. The last one is deliberate: the service's own
source package is `backend/src/tripl/`, so a distribution that installed an
importable `tripl` would shadow it in any environment holding both — a
contributor's backend venv, for one. The service is packaged as the separate
`tripl-server` distribution (tripl-ey6j.6), so `tripl` names this CLI alone.

This package also owns the **shared async REST client** (`tripl_cli.client`) and
the **shared request layer** above it (`tripl_cli.api`): every REST path, query
parameter and response projection either surface uses is spelled once, here.
`tripl-mcp` depends on `tripl` and imports both rather than carrying a copy, and
a contract test in each package fails the build if a path literal appears
anywhere else.

## Install

The only runtime dependency is `httpx`. **Python 3.12+ is required for the
`pip` path only** — `uvx` downloads a suitable interpreter itself, so a host
with `uv` and no Python at all can still run the CLI.

From PyPI — `tripl` is published, so this is the ordinary path:

```bash
uvx tripl --version
# tripl 0.1.0

pip install tripl
```

From a local checkout, when you are working on the CLI itself:

```bash
uv run --project /path/to/tripl/cli tripl --version
```

From git without a checkout — how you get a revision that is not released yet.
It resolves, but it is not exercised by CI:

```bash
uvx --from "git+https://github.com/vladenisov/tripl.git#subdirectory=cli" tripl --version
```

## Commands

A command acting on the instance as a whole is one word; a command acting on a
class of objects is `<plural-noun> <verb>`. Every verb takes `--json` and
`--timeout SECONDS`; the ones that report on many projects also take
`--project SLUG` (repeatable) and `--include-demo`. The ones that name a single
object require `--project` **exactly once**. A bare `tripl scans` or
`tripl drifts` prints that group's help on stderr and exits 2.

```bash
tripl doctor              # check the instance and report what is broken
tripl doctor --json       # one JSON document on stdout, human lines on stderr
tripl doctor --strict     # exit 3 on warnings too (never on skipped checks)
tripl status              # projects, events, scans, signals, coverage
tripl watch               # follow jobs, signals and delivery failures live
tripl watch --json        # JSON Lines on stdout, one object per event
tripl scans list          # scan configs, their schedule, and whether they dispatch
tripl scans jobs <scan> --project SLUG      # recent jobs, newest first
tripl scans run <scan> --project SLUG       # trigger a run now (WRITE)
tripl scans cancel <scan> <job-id> --project SLUG   # cancel an active job (WRITE)
tripl drifts list         # schema drifts; untriaged by default
tripl drifts dismiss <drift-id> --project SLUG      # false_positive or snooze (WRITE)
tripl drifts reopen <drift-id> --project SLUG       # back to open; drops the note (WRITE)
tripl annotate "Deployed web 2026.09.25" --project SLUG --url URL   # deploy marker on monitoring charts (WRITE)
tripl install --app-url https://tripl.example.com --version 1.5.0   # provision a stack and start it (HOST)
tripl install --app-url https://tripl.example.com --dry-run   # print the plan, write nothing
tripl upgrade --to 1.6.0  # move an installed stack to a new image tag (HOST)
```

`--version` defaults to `latest`; pin a released tag in production so a re-run
cannot move you onto an image you have not read the notes for. The command says
so on stderr when you leave it at the default.

`doctor`, `status`, `watch`, `scans list`, `scans jobs` and `drifts list` are
**read-only** — a `tk_r_` key is enough. The five marked WRITE need a `tk_w_`
key backed by an editor or owner, and the CLI does not pre-judge that: the key
prefix is derived from the scope's first letter server-side and says nothing
about the user's role, so the request is sent and the API's own 403 is printed.

`scans cancel`, `drifts dismiss` and `drifts reopen` prompt on a terminal and
take `--yes`; when stdin is **not** a terminal and `--yes` was not given they
refuse with exit 2 rather than hanging a cron job or proceeding silently.
`scans run` and `annotate` do not prompt and have no `--yes` at all — passing one
is exit 2, because a no-op flag here is a flag a script author will assume works
on the next command too. All five writes take `--dry-run`, which resolves everything, prints
the exact request (method, path, params, body — never a credential) and sends
nothing.

`annotate` posts a chart marker with source `api`, for a deploy step in CI. On
this one command `--url` is the release link the marker opens, not the instance:
give the instance with `TRIPL_BASE_URL`, `--base-url`, or `--url` before the
command name. The API de-duplicates the same `api` label within 24 hours (manual
annotations are never de-duplicated) and answers
200 with the existing marker; `annotate` says so and still exits 0, so a retried
job is harmless.

`drifts reopen` is the one whose prompt is worth reading: reopening clears the
drift's `resolution_note`, `resolved_by` and `resolved_at`, and dismissing it
again does not bring them back.

`install` and `upgrade` are the two marked HOST: they act on a **directory and
the local Docker daemon**, not on a running instance, so they take neither
`--url` nor `--api-key` — passing either explicitly is exit 2 rather than a
silently ignored flag. Both take `--dir` (default `./tripl`, always reported
absolute), `--wait SECONDS` (default 300, `0` skips), `--dry-run`, `--yes` and
`--json`, and both are idempotent: `install` re-run converges, and `upgrade` to
the tag already pinned prints `already at X; nothing to do.` and exits 0.

`install` writes `compose.yaml`, `infra/rabbitmq/rabbitmq.conf` and a generated
**0600** `.env` into `--dir`, then runs `docker compose pull` and
`docker compose up -d` in it and polls `<--app-url>/health`. It **never
overwrites an existing `.env`**: that file is created, or appended to with your
confirmation, or left alone. `--force` reaches `compose.yaml` and
`rabbitmq.conf` only, and there is no flag that reaches `.env`, because losing
`ENCRYPTION_KEY` permanently destroys every stored warehouse credential.
`--no-start` writes the files and runs nothing (and skips the Docker probe
entirely). Note that the health poll targets the **public** `--app-url`, so on a
host whose TLS terminator is not up yet, use `--wait 0` and curl
`http://127.0.0.1:8000/health` from the box.

`upgrade --to` is required and a **downgrade is refused outright** with no
override flag; an unorderable pair (`latest`, `sha-abc1234`, `1.4`) says so and
demands `--yes`. It pulls, *then* moves the `TRIPL_VERSION` pin in `.env`
keeping a 0600 `.env.bak.<UTC>` copy, *then* restarts — the pull is first so a
bad tag leaves `.env` untouched. The `pg_dump` backup command is printed for
**you** to run and always prompts: a dump this tool invoked and then called
"your backup" would be a promise it cannot keep, not least because the dump does
not contain `ENCRYPTION_KEY`. Your database lives in the named volume
`pgdata18`, not in `--dir`.

Neither creates the owner account, connects a warehouse or runs the first scan.
The first two are unreachable with an API key of any scope — they need an
interactive owner session — so `install` finishes by reading the instance's real
bootstrap state from the unauthenticated `/auth/status` and printing the URL to
open in a browser.

`docker compose pull` and `up -d` write straight to your terminal and are never
captured, so their progress and their errors are live; the exact invocation is
printed first and is safe to paste. That is why `--json` carries the `argv`, the
`cwd` and the `returncode` of each command rather than its output. No generated
secret is ever printed — the document lists `secrets_generated` by **name**.

Two operations are deliberately **not** offered here. A bounded metrics replay
is owner-**session**-only server-side (`deps.get_owner_user` rejects every
request carrying an API key scope), so no `tripl` command could reach it.
Accepting a schema drift deletes the field definition on a `missing_field`
drift — the damage `doctor`'s `schema_field_deleted_by_accept` finding exists to
report — so that decision stays in the tripl UI. The API refuses the accept
outright (`409`) when a scan config's event name format builds event names from
that column, and its `force` override for that refusal is likewise not spelled
here.

`doctor` runs six checks, always in this order and always exactly once each:
`connectivity`, `auth`, `projects`, `data_sources`, `scans`, `drifts`.
Per-project results are findings *inside* a check, so a consumer selects by
`id` and gets one row.

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

Output is ASCII only and byte-identical whether stdout is a TTY or a pipe, so
`tripl doctor | tee incident.log` and the terminal view are the same artifact.
Two behaviours are worth knowing before you read a report: a **non-200 is never
treated as an empty list** (it becomes `endpoint_unexpected_status`, because a
404 read as "no drifts" is the class of mistake this tool exists to remove), and
the scheduler's **retry backoff is reported as expected behaviour** rather than
as a hang.

`watch` answers the other question: not *what is broken* at one instant, but
*what is happening right now*. It polls (there is no daemon and no subscription)
and prints one line per change — a replay advancing a chunk, a job finishing, a
signal opening, an alert delivery failing to page anyone. It **reaches no
verdict**: a completed run exits 0 whatever it saw, and it never exits 3. The
same ASCII-only, pipe-identical rule applies, so `tripl watch | tee
incident.log` is the artifact you actually saw.

```text
2026-07-31T19:10:41Z  watch.started    1 project, 1 scan config, poll 10s.
2026-07-31T19:10:51Z  job.progress     [prod] 'nightly replay' job job-91c2 chunk 4 of 18 (22.2%) collecting 2026-07-05T00:00:00Z..2026-07-06T00:00:00Z, 2m elapsed.
2026-07-31T19:11:11Z  delivery.failed  [prod] 'Checkout drop' -> slack 'oncall' failed: 'channel_not_found'. Nobody was paged; delivery del-4f21.
2026-07-31T19:11:21Z  watch.stopped    stopped (interrupted) after 40s, 5 ticks, 12 requests.
```

Useful flags beyond the shared three: `--scan NAME_OR_ID` (repeatable, exact
match, narrows the job lines only), `--interval SECONDS` (default 10),
`--duration SECONDS` (stop and exit 0; the default is to run until `Ctrl-C`) and
`--stall-after SECONDS` (default 120, report a running job whose progress has
not moved).

| Exit | Meaning |
|------|---------|
| 0 | Every check passed, or only warned and `--strict` was not given. `status`, whenever it completed. `watch`, whenever the run completed — a failed job or a new signal is still 0. The `scans` / `drifts` verbs and `annotate`, whenever every read arrived or the write was accepted (`--dry-run` included, and a de-duplicated `annotate` too). |
| 1 | The tool itself broke (doctor turns every API failure into a finding), or any other command could not complete a request — unreachable, or the API refused it. For `watch` this includes a key revoked mid-run. For `scans list` / `drifts list` it includes **any** failed read in the fan-out; for `scans run`, a job returned already `failed`; for `scans cancel` / `drifts dismiss` / `drifts reopen`, a declined prompt. |
| 2 | Usage or configuration error. For `doctor` and `status` that is resolved before any socket opens; `watch` also refuses after reading the listings, when `--scan` matches nothing or more than 24 scan configs are selected. The `scans` / `drifts` verbs add a bare group, a missing or repeated `--project`, an unresolved or ambiguous `<scan>`, and a prompting write on a non-TTY without `--yes`; `annotate` adds a `--url` that is not http(s), an `--at` that is not RFC 3339, and half a scope. Either way **no JSON is emitted** and no write is sent. |
| 3 | `doctor` only: at least one check failed, or `--strict` and at least one warning. Nothing else ever exits 3. |
| 130 | Interrupted (SIGINT). For `watch` this is the **normal** ending — a run without `--duration` has no other way to stop. |

An unreachable instance therefore exits **3** out of `doctor`, not 1 — it
becomes a finding like everything else doctor reads, which is what makes an exit
1 out of `doctor` a meaningful bug signal. **Every other command exits 1** on an
unreachable instance, because none of them turns a failed read into a verdict.

`doctor` and `status` put exactly one JSON document on stdout, and so do the
`scans` / `drifts` verbs — but only when the command completes: a write the API
refused, a read that failed outright and a declined prompt all leave stdout
**empty** and put the reason on stderr, so a consumer checks the exit code
before it parses. `watch --json`
puts **JSON Lines**, one object per event, flushed as produced. Within one
`schema_version`, key names are never removed or retyped, and check `id`s,
finding `code`s, `status`/`severity` values and `watch` event tokens are never
renamed or repurposed. New keys, ids, codes and tokens may appear in any
release. `title`, `summary` and `message` are prose. **Assert on `code` and
`evidence` — or, for `watch`, on `event` and `data` — never on prose.**

Full reference — every check, every finding code with its `evidence` keys, every
`watch` event token and the JSON Lines envelope, and what an operator should
actually do about each one:
<https://vladenisov.github.io/tripl/run/cli> (source:
[`website/docs/run/cli.md`](../website/docs/run/cli.md)).

## Configuration

Resolved **per field**, highest precedence first:

1. command-line flag — `--url` / `--base-url`, `--api-key`
2. environment — `TRIPL_BASE_URL`, `TRIPL_API_KEY`
3. config file — `base_url`, `api_key`

Per field, not per source: `--url https://staging` with the key still coming
from the config file works.

| Variable | Meaning |
|----------|---------|
| `TRIPL_BASE_URL` | Base URL of the tripl instance (the same variable `tripl-mcp` reads) |
| `TRIPL_API_KEY` | API key — `tk_r_` for read-only, `tk_w_` for write |
| `XDG_CONFIG_HOME` | Overrides the config file location on every platform |

There is deliberately **no `TRIPL_URL`**: two supported spellings for one
setting is how configuration drifts. If it is set and `TRIPL_BASE_URL` is not,
the error says so by name.

### Config file

| Platform | Path |
|----------|------|
| Linux / BSD / macOS | `$XDG_CONFIG_HOME/tripl/config.toml`, else `~/.config/tripl/config.toml` |
| Windows | `%APPDATA%\tripl\config.toml`, else the `~/.config` fallback |

```toml
# ~/.config/tripl/config.toml
base_url = "https://tripl.example.com"
api_key  = "tk_r_..."
```

TOML because `tomllib` is stdlib on the supported Pythons, so the file format
costs **zero runtime dependencies** — which matters, because `tripl-mcp`
inherits every dependency of this package. Unknown keys and unknown tables are
ignored, so an older CLI keeps working against a file written by a newer one.

`--config PATH` overrides discovery. A missing *default* file is fine; a missing
`--config` path is an error. On POSIX, a config file that holds an `api_key` and
is readable by other users gets one warning on stderr — `chmod 600` it.

Create API keys in the tripl app under **Settings → API keys**.

## Development

```bash
cd cli
uv sync
uv run --group dev pytest -q
uv run --group dev ruff check
uv run --group dev ruff format --check
uv run --group dev mypy src
```

`mcp-server` resolves this package from `../cli` via `[tool.uv.sources]` rather
than from the published release, so a change here is picked up by
`cd mcp-server && uv sync` without any install step — and both suites test the
same source. Run **both** after touching `client.py`.
