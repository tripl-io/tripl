---
title: Searching events
sidebar_position: 3
---

# Searching events

tripl gives you two complementary ways to find events in your tracking plan. They
answer different questions, and using the wrong one wastes time or floods you with
noise:

- **Structured listing** — `GET /events` with substring filters. Use it for
  **exact, precise lookups**: every event carrying a given ticket key, a specific
  field value, a tag, a status, or an event type. Deterministic and repeatable.
- **Smart / semantic search** — `GET /search` with a natural-language query. Use it
  for **feature phrases** when you don't know the exact name: "where do we track
  signup completion?" Ranked by relevance, with a tunable cutoff.

Rule of thumb: if you can name the exact string (a ticket key, an ID, a field
value), use the structured listing. If you're describing a behaviour in words, use
smart search.

## Structured listing — `GET /events`

The listing endpoint applies **substring filters** across the catalog. Every filter
is optional, and you can combine them; results come back as `{items, total}`, with
each item embedding its `field_values`, `meta_values`, and `tags`.

Available query parameters:

| Param | Matches |
| --- | --- |
| `search` | substring over event name and description |
| `field_value` | substring over any field value |
| `meta_value` | substring over any meta value |
| `tag` | events carrying the given tag |
| `event_type_id` | events of a specific event type (a UUID) |
| `status` | events in a given lifecycle status — repeatable |
| `silent_since_days` | events not seen for at least N days |
| `reviewed` | `true` for events marked reviewed, `false` for those not yet reviewed; omit for either |
| `order_by` | `catalog` (the default — the authored catalog order) or `volume` (busiest-first by ingested volume over the last 24h) |
| `offset` | skip the first N items (paging) |
| `limit` | cap the number of items returned (default `200`, max `10000`) |

`status` is the lifecycle filter, and you can pass it more than once to match any of
several states. The valid values are `draft`, `in_review`, `ready_for_dev`,
`implemented`, `live`, `deprecated`, and `archived`; anything else is rejected
with `422` naming the accepted values. There is no separate `implemented` or
`archived` boolean — "implemented" and "archived" are simply `status` values
(e.g. `status=implemented`, `status=archived`).

### Example: every event carrying a ticket key

This is the canonical precise lookup. If your project stores the originating ticket
as a meta value (for example a meta field named `ticket` holding `PROJ-123`), find
every event tied to that ticket with a single `meta_value` filter:

```bash
curl -s \
  -H "Authorization: Bearer tk_r_your_read_key" \
  "https://tripl.example.com/api/v1/projects/web/events?meta_value=PROJ-123"
```

### Example: combine filters

Find implemented events of one type whose name or description mentions "checkout",
limited to the first 20:

```bash
curl -s \
  -H "Authorization: Bearer tk_r_your_read_key" \
  "https://tripl.example.com/api/v1/projects/web/events?search=checkout&event_type_id=b0c2f1e4-7a3d-4c9e-9f01-2a4b6c8d0e12&status=implemented&limit=20"
```

:::tip Use the listing for exact keys and IDs
Ticket keys, field values, and IDs are exact strings. The substring listing matches
them precisely and only returns events that actually carry them.
:::

## Smart / semantic search — `GET /search`

The smart search endpoint takes a natural-language query `q` and returns ranked hits
across the whole project — not just events, but event types, fields, meta
fields, variables, relations, tags, metrics, fact tables, scans and alert
rules. A scan is findable by the columns it is wired to and by the
warehouse SQL it runs, so "which scan reads `checkout_events`" is a search; an
alert rule is findable by the wording of its message template. The response is
`{items, total, truncated, semantic_used}`.

Query parameters:

| Param | Meaning |
| --- | --- |
| `q` | the natural-language query (1–500 characters) |
| `types` | restrict the returned hits to any of the `entity_type` values below — repeatable |
| `include_archived` | include archived entities (default `false`) |
| `semantic` | `false` skips the embedding leg: a keyword-only answer, sooner, with `semantic_used: false` (default `true`) |
| `limit` | cap the number of hits (default `20`, max `100`) |

Each item carries:

- `entity_type` — one of `event`, `event_type`, `field`, `meta_field`, `variable`,
  `relation`, `tag`, `metric`, `fact_table`, `scan_config`, `alert_rule`
- `title`, `subtitle`, `description` (or `snippet`)
- `confidence` — relevance in `0..1`
- `semantic_used` — `true` when the **keyword leg did not surface this row inside
  its candidate window**, so the meaning leg is why it is here. A hit the keyword
  ladder ranked reads `false`, even when its `confidence` is
  taken from the meaning leg's cosine. This is provenance ("why am I looking at
  this row"), not certainty, and it is narrower than the envelope flag of the
  same name — see [The `semantic_used` flag](#the-semantic_used-flag)
- `route_path` — where the entity lives in the app

Event hits additionally include `event_id`, `name`, `implemented`, and
`variable_values` (the observed variable readings for that event).

### Example: a feature phrase

```bash
curl -s \
  -H "Authorization: Bearer tk_r_your_read_key" \
  "https://tripl.example.com/api/v1/projects/web/search?q=where%20do%20we%20track%20signup%20completion&types=event&types=field&limit=10"
```

### Narrowing with `types`

Pass `types` once per entity kind you want to keep. To get only events and the
fields under them:

```text
?q=abandoned%20cart&types=event&types=field
```

### Understanding `confidence` and the cutoff

`confidence` is an **absolute score in `[0, 1]`**, comparable between queries. It is
the hit's relevance measured against the score a result reaches when it *is* the thing
you typed — an exact title match — capped at `1.0`. A query that found nothing good
therefore comes back with low confidence on every item, including the first one.

**`1.0` means the result IS what you typed**, and only that. A hit reaches the top of
the scale only by matching an entity's *identity* — its name being your query, or its
keywords being your query exactly. Everything weaker is partial evidence (the query
appearing as a word inside the name, a stemmed match, a substring somewhere in the
body) and is reported at **`0.8` or below**, however high its underlying relevance
score climbs.

That ceiling matters if you are thresholding: two partial matches that rank differently
can both report `0.8`, because above that line the honest answer is "strong, but not
the thing you named" for both. Use the **order** to tell them apart, not the number.

It used to be normalized to the top hit of the same response, which made the best
result `1.0` by construction — a keyboard mash was served as a perfect answer. If you
built a cutoff against that behaviour, re-check it: thresholds now mean the same thing
on every query, and the numbers are lower than they used to be for weak queries.

When semantic search is on, a hit the **meaning** match found reports that leg's own
cosine similarity instead, which is already a `0..1` certainty. So a result that no
keyword touched but that the vector index is sure about — a misspelling, or a phrase
that describes an event without naming it — is reported as the strong answer it is,
rather than being scaled down for having arrived by the other route. A single cutoff
therefore works across both legs.

Because the tail of a semantic search is always populated with loosely related
results, **trim it with a minimum-confidence cutoff** (a threshold of `0.6` is a
reasonable starting point):

```bash
curl -s \
  -H "Authorization: Bearer tk_r_your_read_key" \
  "https://tripl.example.com/api/v1/projects/web/search?q=user%20completed%20checkout&limit=25" \
  | jq '[.items[] | select(.confidence >= 0.6)]'
```

### `total` and `truncated`

`total` is the number of hits **in this response** — `items | length` — not a
catalog-wide count of everything that matched. `/search` takes no `offset` and
cannot be paged, so there is no second page for a catalog-wide count to size:
to see more hits, raise `limit` (max `100`).

A real pre-paging count is not definable here anyway. The retrieved set is the
union of the keyword predicate and everything above the vector leg's similarity
floor, and that second half fills its window for any query — counting it would
report roughly every embedded entity as a "match". `GET /events` is the endpoint
whose `total` *is* a real count of everything matching, because it pages.

`truncated: true` means ranked hits exist that this response does not carry. On a
semantic answer the ranked tail is effectively unbounded, so `truncated` is
normally `true` and says nothing interesting: **threshold on `confidence`**, as
in the `jq` example above, rather than reading `truncated` as "something is
missing". The flag becomes genuinely discriminating on a keyword-only answer
(`semantic_used: false`), which is where "did I see everything" is a question
with an answer.

### Word forms and plurals

Keyword matching is **stemmed**, in English and in Russian, so a query finds the
other forms of the words you typed: `purchases` finds `purchase_completed`,
`homes` finds the `home` event, and `главных экранов` finds the event whose
description is «Показ главного экрана». Both scripts work in the same project and
even in the same entity — an event named in `snake_case` with a Russian
description is matched from either side.

Two consequences worth knowing:

- Stemming is about word **forms**, not meaning: `purchase` and `buy` are still
  unrelated to keyword matching. That is what the semantic engine is for.
- Exact names still win. A query that IS an entity's name ranks that entity
  first, ahead of anything that merely stems to it.

### Names outrank descriptions

A match in an entity's **name** counts for more than the same word buried in its
description, its field values, or the values a scan harvested from your warehouse.
So searching `paywall` surfaces the event *called* paywall-something ahead of an
unrelated variable that merely happens to have observed the string `paywall` in
production data.

This matters most for **auto-detected variables**, which accumulate whatever your
app emitted. Before, a variable holding a common word thousands of times could
outrank the event actually named after it. It no longer can.

A caveat worth knowing: entities whose names are in one language are not favoured
by queries in another. An event named `catch_report_created` gains nothing from a
Russian query, while a field displayed as `Тип улова` does — so for a mixed-language
plan, search in the language the thing is *named* in.

### The `semantic_used` flag

There are two flags with this name and they answer two different questions.

**On the envelope**, `semantic_used` tells you which engine answered:

- `true` — embeddings were used (true semantic ranking by meaning).
- `false` — the instance has no embedding provider configured, so search fell back
  to keyword/substring matching. `/search` still works, but it ranks by word
  overlap (stemmed, as above) rather than by meaning.

**On each item**, `semantic_used` is narrower: it is `true` when the keyword leg
did not rank that hit inside its candidate window, so the meaning leg is what
produced it. A document the keyword ladder also found reads
`false`, because the answer to "why is this row here" is the ladder. The vector
leg is a nearest-neighbour scan that returns rows for any query, so "the vector
index also listed this row" is a fact about the window, not about the row.

It is not a claim that no keyword matched. The keyword leg is a capped scan too,
so a weak match — a stem-only match, which earns no exact-match tier — can be
crowded out of its window and re-enter through the meaning index, reading `true`
on a row the keyword index did match. Read the flag as *the keyword ranking did
not surface this*, never as *the word is not there*.

The two therefore **disagree on the same response all the time, and that is the
normal case rather than a fault**: an envelope reading `semantic_used: true`
with every item reading `false` just means the semantic leg ran and the keyword
leg had already found everything it ranked. Diagnose an instance's embedding
configuration from the envelope, never from a row.

A per-item `false` also says nothing about where that item's `confidence` came
from. `confidence` reports the stronger of the two certainties, so a hit found by
both legs can honestly answer `0.9` to "how sure are you" and "the keyword leg"
to "how did you find it".

Semantic ranking normally requires an embedding provider. See
[AI and search configuration](../run/ai-and-search.md) for how to enable it (and what
the keyword fallback behaves like when it's off).

:::note Demo project exception
The **demo project** ships with precomputed embedding vectors for its own
content and a small set of suggested queries, so demo searches can return
`semantic_used: true` even on an instance with **no** embedding provider
configured. Don't use demo-project responses to diagnose an instance's
embedding configuration — check a regular project (or the settings) instead.
:::

## When NOT to use smart search

:::warning Don't use smart search for exact keys or IDs
A semantic query for an exact code (a ticket key, an event ID, a field value)
**floods** the results: the exact string is loosely related to many events, so the
ranking surfaces dozens of weak matches instead of the one you want. For exact keys
and IDs, always use the **structured listing** (`GET /events`) with `meta_value`,
`field_value`, or `search` — it returns only the events that actually carry the
string.
:::

## Choosing a mode

| You want to… | Use |
| --- | --- |
| Find every event tagged to ticket `PROJ-123` | listing — `meta_value=PROJ-123` |
| Find events with a specific field value | listing — `field_value=…` |
| List implemented events of one type | listing — `event_type_id=…` + `status=implemented` |
| List not-yet-implemented events of one type | listing — `event_type_id=…` + `status=draft` |
| Find "where do we track signup completion?" | smart search — `q=…` |
| Discover related fields/variables for a feature | smart search — `q=…&types=event&types=field` |
