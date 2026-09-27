---
title: Driving tripl from an agent or script
sidebar_position: 2
---

# Driving tripl from an agent or script

tripl is the canonical catalog of your analytics events — the single source of
truth for what each event means, which fields it carries, and which task it came
from. That makes it a natural backend for an LLM agent or a plain automation
script: read the catalog before you act, draft new event definitions, then write
them back safely.

This page covers the REST surface, authentication, and the
**read → draft → write** loop. For natural-language discovery of existing
events, see [Searching events](./searching-events.md). For an end-to-end worked
example, see the [Agent API guide](../integrate/agent-api-guide.md).

:::tip
Agents running in an MCP-capable runtime (Claude Code, Claude Desktop, or
another MCP client) should prefer the first-party
[MCP server](../integrate/mcp-server.md), which wraps this API in curated,
safety-annotated tools. The REST recipes below remain the right path for
direct HTTP integrations.
:::

## Getting an API key

Create keys in the app under **Settings → API keys**. The full token is shown
**once** at creation — copy it then; afterwards only the non-secret prefix is
visible. Every request authenticates with a bearer token:

```
Authorization: Bearer <api_key>
```

A key can be scoped to a single project or left able to see every project your
account can reach.

### Read vs write scopes

The scope letter is embedded in the token prefix so you (and your logs) can tell
at a glance what a key can do:

| Scope | Token prefix | Can call |
|-------|--------------|----------|
| `read` | `tk_r_…` | Read/query operations only; mutation endpoints return `403` (some complex read-only queries use `POST`). |
| `write` | `tk_w_…` | Everything a read key can, plus create / update / delete. |

:::tip
Give a discovery-only agent a `tk_r_` key. Reserve `tk_w_` keys for the
write step, and prefer a project-scoped key so a bug can't touch the wrong
project.
:::

## REST surface

Everything lives under `/api/v1`. `{slug}` is the project slug (for example
`web`).

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| `GET` | `/projects` | read | List projects: `[{slug, name}]` |
| `GET` | `/projects/{slug}/event-types` | read | Event types; each embeds its `field_definitions` (`id`, `name`, `display_name`, `field_type`, `required`, enum options) |
| `GET` | `/projects/{slug}/meta-fields` | read | Project meta fields |
| `GET` | `/projects/{slug}/events` | read | Filtered listing → `{items, total}`; each item embeds `field_values`, `meta_values`, `tags` |
| `GET` | `/projects/{slug}/events/{id}` | read | Full event detail |
| `GET` | `/projects/{slug}/search` | read | Smart / global search → `{items, total, truncated, semantic_used}`. Unlike the `/events` row above, this `total` counts only the hits in *this* response — `/search` cannot be paged, so `truncated` is the completeness signal |
| `POST` | `/projects/{slug}/events` | write | Create an event |
| `PATCH` | `/projects/{slug}/events/{id}` | write | Update an event |
| `DELETE` | `/projects/{slug}/events/{id}` | write | Permanently delete an event |

### Listing filters (`GET /events`)

All string filters match as a case-insensitive substring. Combine them freely:

| Query param | Matches |
|-------------|---------|
| `search` | Substring over event name / description |
| `field_value` | Substring over any field value |
| `meta_value` | Substring over any meta value (e.g. a ticket key) |
| `tag` | Exact tag |
| `event_type_id` | Events of one event type |
| `status` | One or more lifecycle states (repeatable). Values: `draft`, `in_review`, `ready_for_dev`, `implemented`, `live`, `deprecated`, `archived` — any other value is a `422` |
| `silent_since_days` | Events with no observed traffic for at least N days |
| `reviewed` | `true` for events marked reviewed, `false` for those not yet reviewed. Omit for either. Independent of `status` — an event can be reviewed and still be `in_review` |
| `order_by` | `catalog` (the default — the authored catalog order) or `volume` (busiest-first by ingested volume over the last 24h); any other value is a `422` |
| `offset`, `limit` | Pagination (`limit` defaults to `200`, max `10000`) |

:::note
There is no boolean `implemented` or `archived` filter — those are lifecycle
**statuses**. To list implemented events, pass `status=implemented`; to find
retired ones, pass `status=archived`.
:::

### Search params (`GET /search`)

| Query param | Meaning |
|-------------|---------|
| `q` | Natural-language phrase (1–500 chars) |
| `types` | Restrict to entity kinds (repeatable): `event`, `event_type`, `field`, `meta_field`, `variable`, `relation`, `tag`, `metric`, `fact_table`, `scan_config`, `alert_rule`, `doc` |
| `include_archived` | Include archived entities (default `false`) |
| `limit` | Max results (default `20`, max `100`) |

Each hit carries a `confidence` in `[0, 1]` (an absolute relevance score, comparable
between queries — a weak query returns low confidence on every item, first one
included; a hit found by meaning rather than by keyword reports that leg's cosine
similarity, so one cutoff works for both) and `semantic_used` reports whether
embeddings were used — that is the **envelope** flag, a fact about the instance's
configuration.

Each hit carries its own `semantic_used` too, and it means something narrower:
the keyword leg did not surface this row inside its candidate window, so the
meaning leg is why it is here. A hit the keyword ladder ranked reads `false` even
when its `confidence` came from the meaning leg's cosine, so the envelope and the
items routinely disagree on one response. That is the normal case, not a fault:
read the envelope to answer "are embeddings on", and a row to answer "why is this
row here". Do not read a row's `true` as "the word is not there" — the keyword
leg is a capped scan, so a weak match crowded out of it can re-enter through the
meaning index and carry the flag.

`total` is the size of *this* response, not a count of everything that matched:
`/search` takes no `offset` and cannot be paged, so raise `limit` (max `100`) to
see more. `truncated` is the completeness signal — but with `semantic_used: true`
the ranked tail is effectively unbounded and `truncated` is normally `true`, so
an agent should gate on `confidence` rather than treat the flag as "results are
missing".

If your agent branches on confidence, know the shape of the scale: `1.0` is reserved
for a hit whose **identity** is the query (its name, or its keywords, being exactly
what you searched for). Hits without an identity match are capped at `0.8`,
including semantic hits. Thus `>= 0.9` indicates an identity match, while a
lower confidence can still be relevant.
Event hits also include `event_id`, `name`, `implemented`, and
`variable_values`. Use search for feature phrases, not exact keys — see
[Searching events](./searching-events.md).

## Minimal auth example

`curl`:

```bash
curl -s https://tripl.example.com/api/v1/projects \
  -H "Authorization: Bearer $TRIPL_API_KEY"
```

Python with `requests`:

```python
import os
import requests

BASE = "https://tripl.example.com/api/v1"
session = requests.Session()
session.headers["Authorization"] = f"Bearer {os.environ['TRIPL_API_KEY']}"

resp = session.get(f"{BASE}/projects")
resp.raise_for_status()
for project in resp.json():
    print(project["slug"], "-", project["name"])
```

## The read → draft → write loop

The agent pattern is always the same: **read** the catalog to find what already
exists, **draft** definitions in memory, then **write** them back safely.

### 1. Read first

tripl is the source of truth, so never create blind. Locate relevant events by
the originating ticket (a `meta_value` filter), by a feature phrase (smart
search), or by structured filters — *before* acting.

```python
# Every event already linked to a ticket, by its key in the "ticket" meta field.
existing = session.get(
    f"{BASE}/projects/web/events",
    params={"meta_value": "PROJ-123"},
).json()
by_name = {item["name"]: item for item in existing["items"]}
```

### 2. Draft

Build the payload in memory. An event references its event type, carries a clear
description, field values, and meta values — including a link back to the ticket
it came from so the event stays retrievable by task.

```python
def build_event(event_type_id, ticket_meta_field_id):
    return {
        "event_type_id": event_type_id,
        "name": "checkout:completed",
        "description": "Fires once the order is confirmed.",
        "status": "draft",
        "tags": ["checkout"],
        "field_values": [],
        "meta_values": [
            {"meta_field_definition_id": ticket_meta_field_id, "value": "PROJ-123"},
        ],
    }
```

### 3. Write safely

Validate before you POST. A **dry run** builds and prints the payload without
sending it, so a human (or a test) can eyeball it first:

```python
def upsert_event(payload, by_name, dry_run=True):
    if dry_run:
        print("DRY RUN — would write:", payload)
        return None

    existing = by_name.get(payload["name"])
    if existing:  # exact-name upsert avoids duplicates
        return session.patch(
            f"{BASE}/projects/web/events/{existing['id']}",
            json=payload,
        ).json()
    return session.post(
        f"{BASE}/projects/web/events",
        json=payload,
    ).json()
```

Key safety rules baked into the loop:

- **Dry-run by default.** Build and print the payload; only switch
  `dry_run=False` once it looks right.
- **Upsert by exact canonical name.** Match on the event `name` you already read
  back. If it exists, `PATCH` it; otherwise `POST`. When the target event type
  has a scan `event_name_format`, the server generates that canonical name from
  field values: supply every template field and use the name/id returned by the
  mutation rather than assuming the proposed name was kept.
- **Read mutation warnings.** Event create/update responses include `warnings`.
  A different proposed name may be ignored because a scan naming rule owns the
  identity; unknown `${variable}` tokens are also advisory contract problems to
  surface to the operator.
- **Attach the originating ticket.** Always set a `ticket` meta value (e.g.
  `PROJ-123`). That single link is what lets the next run find the event by task
  rather than guessing.

### Archiving obsolete events

When an event should leave the active plan but stay in the catalog for history,
**archive** it by setting its status rather than deleting it:

```python
session.patch(
    f"{BASE}/projects/web/events/{event_id}",
    json={"status": "archived"},
)
```

`DELETE /projects/{slug}/events/{id}` permanently removes the event and its
field/meta values — reach for it only when you truly want the record gone.
Archiving keeps the event findable and preserves the link back to its ticket.

## Next steps

- [Searching events](./searching-events.md) — natural-language discovery and
  when to prefer structured filters.
- [Agent API guide](../integrate/agent-api-guide.md) — a fuller worked
  integration.
