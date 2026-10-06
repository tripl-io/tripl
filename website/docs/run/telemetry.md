---
title: Telemetry
sidebar_position: 9
---

# Telemetry

Tripl can send one small, anonymous ping a day, so the people who build it
know how many instances keep running and on which warehouses. It is **off**
unless you turn it on, and nothing in it names a person, a team, a host or
your data.

## Turning it on or off

- `tripl install --telemetry` writes `TELEMETRY_ENABLED=true` into the `.env`
  it creates; without the flag it writes `false`.
- Anywhere else, set `TELEMETRY_ENABLED=true` (or `false`) in the server's
  environment and restart. `TELEMETRY_ENDPOINT` says where the ping goes
  (`https://telemetry.tripl.io/v1/ping` by default); empty sends nothing.

A public demo instance (`PUBLIC_DEMO`) never sends it, whatever these say.

## What it sends

Once a day, at 07:13 UTC, the worker POSTs this JSON document — and nothing
else:

| Field | Example | What it is |
|---|---|---|
| `schema` | `1` | The document's version. |
| `instance_id` | `"5f0c…"` | A random id, made the first time and kept in the database. It identifies the instance, not anyone using it. |
| `version` | `"0.2.3"` | The server's version. |
| `edition` | `"community"` | `community` or `enterprise`. |
| `deployment_mode` | `"self_hosted"` | `self_hosted` or `hosted`. |
| `warehouse_engines` | `["clickhouse"]` | Which engines your data sources use (`bigquery`, `clickhouse`, `databricks`, `postgres`, `synthetic`); never their hosts or names. |
| `projects` | `"1-10"` | How many projects, as a range: `0`, `1-10`, `11-100`, `101-1000` or `1000+`. |
| `event_types` | `"11-100"` | How many event types, as a range. |
| `users` | `"1-10"` | How many accounts, as a range. |
| `scans_last_day` | `"0"` | How many scans ran in the last 24 hours, as a range. |
| `days_since_install` | `42` | Days since the instance made its id. |

Never sent: names, slugs, email addresses, hosts, URLs, queries, event or
field names, or anything read from your warehouse.

## Seeing what was sent

**Settings → Platform → Runtime** shows, to platform admins, whether
telemetry is on, where it goes, and the exact document it last sent
(`GET /api/v1/platform/settings/telemetry`).

## When the receiver is down

The request has a five-second timeout and follows no redirect. A failure is
logged at `INFO` and forgotten; nothing retries, and nothing else is affected.
