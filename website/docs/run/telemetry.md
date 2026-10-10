---
title: Telemetry
sidebar_position: 9
---

# Telemetry

tripl sends one small, anonymous ping a day, so the people who build it know
how many instances keep running and on which warehouses. Nothing in it names
a person, a team, a host or your data, and you can turn it off with one
setting.

## On or off

- **Community:** on by default. **Enterprise:** off by default. Enterprise
  means the Enterprise package is installed: a Community server that runs
  another extension (your own, or a third party's) is still Community, reports
  `community` and keeps the default-on ping.
- When it is on, the API says so at startup, in its log, with how to turn it
  off:

  ```
  Anonymous usage telemetry is ON: one small ping a day to https://telemetry.tripl.io/v1/ping - no names, emails, hosts, queries or data. What it holds: https://docs.tripl.io/run/telemetry. Turn it off with TELEMETRY_ENABLED=false (or DO_NOT_TRACK=1) and restart.
  ```

- **Turn it off:** set `TELEMETRY_ENABLED=false` in the server's environment
  (the `.env` next to `compose.yaml`) and restart. `DO_NOT_TRACK=1`, the
  cross-tool opt-out, turns it off too, whatever `TELEMETRY_ENABLED` says.
- **Turn it on** in Enterprise: `TELEMETRY_ENABLED=true`.
- `tripl install --no-telemetry` writes `TELEMETRY_ENABLED=false` into the
  `.env` it creates, and `--telemetry` writes `true`. Without either, `install`
  leaves the edition's default and says on its output that telemetry is on.
  `--no-telemetry` needs a `tripl` CLI newer than 0.3.1; the 0.3.1 CLI
  writes `TELEMETRY_ENABLED=false` unless you pass `--telemetry`.
- `TELEMETRY_ENDPOINT` says where the ping goes
  (`https://telemetry.tripl.io/v1/ping` by default). Empty or unset means that
  default, so it is not an off switch: use `TELEMETRY_ENABLED=false` or
  `DO_NOT_TRACK=1` and restart.

A public demo instance (`PUBLIC_DEMO`) never sends it, whatever these say, and
neither does the development stack (`compose.dev.yaml` sets
`TELEMETRY_ENABLED=false`).

## What it sends

Once a day, at 07:13 UTC, the worker POSTs this JSON document — and nothing
else:

| Field | Example | What it is |
|---|---|---|
| `schema` | `1` | The document's version. |
| `instance_id` | `"5f0c…"` | A random id, made the first time and kept in the database. It identifies the instance, not anyone using it. |
| `version` | `"0.2.3"` | The server's version. |
| `edition` | `"community"` | `enterprise` when the Enterprise package is installed, otherwise `community` (also with another extension loaded). |
| `deployment_mode` | `"self_hosted"` | `self_hosted` or `hosted`. |
| `warehouse_engines` | `["clickhouse"]` | Which engines your data sources use (`athena`, `bigquery`, `clickhouse`, `databricks`, `greenplum`, `postgres`, `redshift`, `snowflake`, `synthetic`, `trino`); never their hosts or names. |
| `projects` | `"1-10"` | How many projects, as a range: `0`, `1-10`, `11-100`, `101-1000` or `1000+`. |
| `event_types` | `"11-100"` | How many event types, as a range. |
| `users` | `"1-10"` | How many accounts, as a range. |
| `scans_last_day` | `"0"` | How many scans ran in the last 24 hours, as a range. |
| `days_since_install` | `42` | Days since the instance made its id. |

Never sent: names, slugs, email addresses, hosts, URLs, queries, event or
field names, or anything read from your warehouse.

## Seeing what was sent

**Settings → Platform → Runtime** shows, to platform admins, whether
telemetry is on (and if not, why), where it goes, and the exact document it last sent
(`GET /api/v1/platform/settings/telemetry`).

## What happens to it

The receiver keeps one row per instance per day — the fields above and the
date, nothing about the request it came in (no address, no headers) — and
deletes rows after 90 days.

## When the receiver is down

The request has a five-second timeout and follows no redirect. A failure is
logged at `INFO` and forgotten; nothing retries, and nothing else is affected.
