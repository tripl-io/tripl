---
title: Project health and search across projects
sidebar_position: 2.5
---

# Project health and search across projects

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md).
:::

Two pages look at all of an organization's projects at once: **Project
health**, a table of what is failing or open in each project, and **Search
projects**, one search over every project. Both are in **Settings →
Organization**, right after **Details**, for the organization's owners and
admins. Owners and admins see every project of the organization.

Their API answers any member of the organization, and only ever about the
projects that member can open: a project they are not a member of never adds a
row, a count or a search hit.

## Project health {#project-health}

**Settings → Organization → Project health** lists every project with:

| Column | What it counts |
|---|---|
| Incidents | Open incidents in the project's alerting inbox; the number links to it |
| Firing | Monitors firing now |
| Failing scans | Scans whose latest run failed |
| Drifts | Open property drifts nobody has triaged |
| Anomalies | Open anomaly signals |
| Coverage | Implemented events out of active ones, printed as on the project's own Coverage page (one decimal; 100% only when every active event is implemented; a dash with no active event) |
| Failed runs, 7d | Failed scan runs over the last 7 days, out of all runs, with one bar per day |
| Last scan | When the newest scan run ended |

The counters are the same ones each project's own pages show (the project
list's `summary`), so the table and a project's Overview agree. The table
starts with the most open incidents first; click a column header to sort by it,
and click it again to reverse the order. The project name opens the project.

Above the table, the organization's totals: how many projects need attention
(any failing scan, firing monitor, open incident or open drift), the open
incidents, firing monitors, failing scans and open drifts across all of them,
the plan coverage over every project's events together, and the failed scan
runs of the last 7 days.

The 7-day columns count by UTC day. If they cannot be loaded, the counters are
still shown, with a note.

## Search projects {#search-projects}

**Settings → Organization → Search projects** searches every project of the
organization the person searching can open, each on its **main** plan, and
lists the best matches first, each labelled with its project. A result opens
the event, property, metric, scan, alert rule or docs note in its project.

- The search matches words and names, as each project's own search does, but
  without its meaning-based matching; to search by meaning, open the project
  and use its search.
- At most 50 results are shown. When more match, the page says so; narrow the
  search to see them.
- At most 50 projects are searched, in name order. When an organization has
  more, the page says how many were not searched.
- Docs notes hidden from the person searching are never found.
- The query is kept in the page address (`?q=`), so a search can be shared or
  reloaded.

## Who can see what

| | Owners | Admins | Members | API keys of the organization | Project API keys |
|---|---|---|---|---|---|
| The two pages in Settings | yes | yes | no | — | — |
| The API, over the projects they can open | yes | yes | yes | yes | no (`403`) |

A user who is not in the organization gets `404`.

## API {#api}

| Method | Path | Who |
|---|---|---|
| `GET` | `/api/v1/orgs/{org}/insights/trends` | any member of the organization, or an API key of it |
| `GET` | `/api/v1/orgs/{org}/insights/search` | any member of the organization, or an API key of it |

### Trends

`GET /api/v1/orgs/{org}/insights/trends?days=7` takes `days` from 1 to 30
(default 7) and answers each visible project's daily counts, oldest day first,
today last:

```json
{
  "days": ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"],
  "projects": [
    {
      "project_id": "6f1c…",
      "slug": "web",
      "name": "Web",
      "scan_runs": [4, 4, 4, 5, 4, 4, 2],
      "failed_scan_runs": [0, 0, 1, 0, 0, 2, 1],
      "alert_deliveries": [0, 1, 0, 0, 0, 3, 1]
    }
  ]
}
```

Days are UTC. `scan_runs` counts the scan runs queued that day,
`failed_scan_runs` those that failed, and `alert_deliveries` the alert
deliveries sent or attempted. The current counters (open incidents, firing
monitors, drifts) are in `GET /api/v1/orgs/{org}/projects`, under each
project's `summary`.

### Search

`GET /api/v1/orgs/{org}/insights/search?q=checkout` takes:

| Parameter | |
|---|---|
| `q` | the query, 1 to 500 characters (required) |
| `limit` | results in all, 1 to 100, default 20 |
| `types` | repeatable; only these kinds of result (`event`, `event_type`, `field`, `meta_field`, `variable`, `relation`, `tag`, `metric`, `fact_table`, `scan_config`, `alert_rule`, `doc`) |
| `include_archived` | include archived events (default `false`) |

It answers:

```json
{
  "items": [
    {
      "entity_type": "event",
      "title": "checkout_completed",
      "route_path": "/o/acme/p/web/events/…",
      "score": 3.0,
      "confidence": 1.0,
      "project": { "id": "6f1c…", "slug": "web", "name": "Web" }
    }
  ],
  "total": 1,
  "truncated": false,
  "projects_searched": 3,
  "projects_skipped": 0
}
```

Each item is a result of the project's own search
(`GET /api/v1/projects/{slug}/search`, with `semantic=false`) plus the
`project` it came from; the example leaves most of its fields out. `truncated`
is `true` when more results match than are returned. `projects_skipped` counts
the projects the caller can open that were not searched, past the first 50.
