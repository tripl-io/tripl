# Agent API Guide

This guide describes the recommended way for external LLM agents and CLI scripts to consume the tripl API.

## Recommendation

Use the existing FastAPI OpenAPI contract plus this guide as the primary agent integration path.

- Machine-readable contract: `GET /openapi.json`
- Interactive contract browser: `GET /docs`
- Base API prefix: `/api/v1`

tripl now ships a first-party MCP server (`tripl-mcp`) that wraps this API in a curated toolset for MCP-capable agent runtimes — see [MCP Server](./mcp-server.md) for setup. This guide remains the raw REST contract underneath it: every MCP tool calls the endpoints described here with the same API-key auth, project fencing, and branch rules. Use the MCP server when the agent runs in an MCP-capable runtime; use raw OpenAPI plus this guide for direct HTTP integrations, scripts, and anything the curated toolset does not cover.

## Base URL

The published document carries no `servers` block, by design. A client therefore
resolves every path against the URL it fetched the spec from: an instance reached
at `https://tripl.example.com/openapi.json` is called at
`https://tripl.example.com/api/v1/...`, and the same build reached at
`http://localhost:8000` in development is called there. Nothing to configure, and
no server-side setting can point your client at a different host than the one you
already reached.

Two consequences worth knowing:

- In `/docs`, **Try it out** calls the origin the page is open on. That is a
  same-origin request, so it works regardless of the instance's CORS allow-list.
- Code generators that insist on an absolute base URL substitute their own
  placeholder (often `http://localhost`) when `servers` is absent. Set your
  origin on the generated client's configuration instead of expecting the spec to
  carry it. The same applies to the committed `backend/openapi.json` in the
  repository, which is the same document with no retrieval URL to resolve against.

## MCP Server

For agents running in MCP-capable runtimes (Claude Code, Claude Desktop, and other MCP clients), `tripl-mcp` packages a curated read/write toolset on top of this API: stdio and streamable-http transports, `readOnlyHint` annotations on read tools, `tk_w_` key requirements on write tools, a mandatory `branch_id` on plan-mutating tools, and a `TRIPL_MCP_ALLOW_MAIN` gate that keeps agents off the main branch by default. Installation, transport configuration, and the full tool list live in [MCP Server](./mcp-server.md). Everything below documents the underlying REST contract that the MCP tools share.

## Authentication

Agents should authenticate with user-issued API keys:

```http
Authorization: Bearer tk_...
```

API keys are created by an authenticated user through:

```http
POST /api/v1/me/api-keys
```

Creation payload:

```json
{
  "name": "docs-agent",
  "scope": "read",
  "expires_in_days": 90,
  "project_slug": "demo"
}
```

Scopes:

- `read`: read-only. Mutation endpoints reject it, while read/query operations
  remain available even when an endpoint uses `POST` for a complex query body.
  Use this for retrieval, search, and agent context loading.
- `write`: allowed on mutation endpoints, subject to the user role behind the key. Editor-only routes still require an editor or owner user.
- Owner-only security and instance-administration routes require an interactive owner session; an API key is `403` on them even when its user is an owner. The one exception is the [metrics replay](#replaying-metrics), which a `write` key backed by an owner may call.

Project scope:

- `project_slug` binds the key to one `/projects/{slug}/...` namespace.
- Project-scoped keys cannot call instance-level routes such as `/api/v1/projects` or `/api/v1/users`.
- Omit `project_slug` only for trusted automation that must read or write multiple projects.

If a Bearer token is invalid, expired, or revoked, the API returns `401`. If a valid key lacks project, scope, or role permission, the API returns `403`.

## Project And Branch Context

Most agent calls require a project slug in the path:

```text
/api/v1/projects/{slug}/...
```

Plan branch context is passed as the query parameter named `branch`:

```text
?branch=<branch_id>
```

If `branch` is omitted, services resolve the project's main branch. For read-only context gathering, omitting `branch` is usually correct. For proposed edits, pass the working branch id explicitly so the agent does not mutate the live plan by accident. Passing the main branch's own id is the same as omitting `branch`.

A merged branch is read-only, and a closed one is read-only until it is reopened: a write naming either answers `409` (`Branch '<name>' is merged, so its plan is read-only` or `Branch '<name>' is closed; reopen it before editing its plan`). Reads still work on both, and so does `POST /api/v1/projects/{slug}/search/reindex`; the AI `describe-event` and `describe-event-type` suggestions, which write nothing, are refused like writes. The `409` only reaches a caller the route would let write: a viewer or a `read`-scope key gets the route's own `403` first. On a branch-scoped write, `401` comes first, the route's `403` before the `409`, and the `409` before the route's own `404`s and body-schema `422`s; whether a malformed or foreign `branch` (`400` / `404`) or the `403` answers first depends on the route. A body sent with a JSON `Content-Type` (`application/json` or `application/*+json`) that is not valid JSON is a `422` before any of these, authentication included; under any other `Content-Type`, or none, the body is only checked against the route's schema after them.

The photo and Figma spec writes (`POST /api/v1/projects/{slug}/events/{event_id}/photos`, `POST .../photos/figma`, `PATCH .../photos/reorder` and `DELETE .../photos/{photo_id}`) take no `branch`: they address a branch's event by its own id. They answer the same `409` when that event belongs to a merged or closed branch. Their order differs, and two of the refusals come before authentication rather than after it: the whole router caps the request body, so an upload declaring a `Content-Length` over `PHOTO_MAX_SIZE_MB` — or streaming past it — is `413` before any dependency runs, `401` included, and a malformed JSON body on `photos/figma` or `photos/reorder` is `422` in the same place. Everything after that is dependency-ordered: `401`, then the route's `403`, then the `404` for an unknown event, then the `409`, and only then the file's own `415` / `422`, the `404` for an unknown photo or the `400` for an incomplete reorder list. Comments, on a photo or on the event, are not plan content and are accepted on any branch.

Plan writes and merges never interleave. A write to a branch that is being merged (a `?branch=` write, a revert, or a photo or Figma spec write) waits until the merge commits and then answers the `409` above; a merge that starts while a write to its branch is in flight waits for it, so a write after the approval makes the merge answer `409` `insufficient_approvals` with the approval counted as stale. A write to main waits for any merge in progress and then applies on top of the merged plan, and a merge that starts during a write to main waits for it and reports a `409` `conflicts` where the two disagree, rather than overwriting it. A comment on a branch's event posted during that branch's merge waits as well, then lands in the thread on main. The wait lasts as long as the merge takes, which grows with the size of the plan; the AI `describe-event` and `describe-event-type` suggestions, which write nothing, never wait.

Passing `branch` also makes the write **attributable**: the audit log records the entry against that working branch, by id and by name, and an owner reading the log sees a branch chip on the row. A write with no `branch` carries no chip, which covers both a deliberate write to main and an action that has no branch dimension at all — and passing the **main** branch's own id records no branch either, by design, so one write to main cannot render two ways. So an agent's branch-scoped edits are distinguishable after the fact from writes to the live plan — which is the other reason to pass the id rather than rely on the default. This applies to the branch-scoped plan writes (`event.*`, `field.*`, `event_type.*`, `variable.*`, `meta_field.*`, `relation.*`, and `project.retire_unused_variables`). Drift resolutions (`variable.drift_action`, `schema_drift.*`) are the exception: a drift is only ever detected against main, so accepting one is always a write to the main plan and carries no chip whatever `branch` you pass. Event writes are recorded as `event.create`, `event.bulk_create`, `event.update`, `event.bulk_update`, `event.delete` and `event.bulk_delete`; a bulk route files one row per request, with the ids (and, for a delete, the names) in the payload. Reordering an event is not recorded, and neither are events written by a scan — but accepting a scan's shadow-event candidate is a plan write, not a scan write, and files `event.create` like any other, with the candidate it was admitted from named in the payload; dismissing one files `shadow_event.dismiss` against the candidate and carries no branch, a candidate having no branch to name. Events additionally keep their own per-event history (`GET /projects/{slug}/events/{event_id}/history`): a `created` row first, then before/after rows keyed `status`, `name`, `title`, `description`, `sunset_at`, `tags`, `field:<field name>` and `meta:<meta field name>`. That history is removed with the event, while the audit row is not — so a deleted event's `field_values` are recoverable from neither surface.

Discover branches:

```http
GET /api/v1/projects/{slug}/branches
```

The response includes each branch `id`, `name`, `kind`, and `status`. Use the `id` as the `branch` query parameter on plan endpoints.

Add `?include_diff_counts=true` when you need a per-branch summary rather than the branches themselves. Each open working branch (`draft`, `ready_for_review`, `changes_requested` or `approved`) then also carries `ahead` (how many entities it changed against its base, counting a rename as one change, as the branch's diff view does) and `behind_base` (whether main moved since the branch was cut), computed for the whole list from a single main snapshot — one request instead of a `/diff` call per branch. Merged and closed branches keep both `null`, like main. It is opt-in because building those snapshots is the expensive part of the response; leave it off when you only need the branch rows.

Create a branch with `POST /api/v1/projects/{slug}/branches` (editor role). It answers `409` in two cases that only `detail` tells apart: `Branch with this name already exists`, and `The project changed while the branch was being created. Please try again.` The second is rare: on Postgres the server already retries a creation the database aborts as unserializable (in practice, a project rename landing in the same instant), up to three attempts, and answers `409` only when all three are aborted. Nothing of a failed attempt is kept, so send the same request again.

Review what a working branch changed, and undo one change of it:

```http
GET  /api/v1/projects/{slug}/branches/{branch_id}/diff
POST /api/v1/projects/{slug}/branches/{branch_id}/revert
```

The diff returns one entry per changed entity, each carrying `entity_type`, `kind` (`added` / `changed` / `removed`), `name`, `parent`, the `entity_id` it describes, and — for a changed entity — `field_changes`. A collection-valued field there additionally breaks down into `items`, keyed by the member that moved (a field name, a tag, the event an override targets).

Names are not always unique: two events can share a type and name, and two relations can link the same two fields. Each branch copy records the `main` row it was made from, so the diff, the merge and a revert pair such rows one by one: each gets its own entry, and an entry's `entity_id` (the branch row, or the base row for a removal) tells them apart. Only on a branch opened before copies recorded their origin can a name still stand for several rows the server cannot tell apart; an entry for such a name carries a warning in `warnings` telling you to rename one of the events, or remove one of the relations, before changing either.

Read the response's `renames` list before interpreting those entries. Entities are keyed by name, so a rename arrives split in two — a removal of the old name beside an addition of the new one — which reads as a deletion your agent never made. Each `renames` element (`entity_type`, `parent`, `removed_name`, `added_name`) names the two entries the merge will treat as **one** renamed row, keeping the entity's id and everything hanging off it. The pairing is stated by the server because it also depends on `main`, which the diff you are holding does not show.

`revert` takes the coordinates of one such entry and restores it to the branch's base state, responding with the resulting diff:

```json
{ "entity_type": "event", "name": "purchase:success", "parent": "track", "field": "field_values", "entity_id": "5a1f…" }
```

Pass the entry's `entity_id` as well: when two entries share a name it is the only thing that says which one you mean, and without it such a name is refused with `409` (`More than one change on this branch is called …`). Omit `field` to revert the whole entity: an addition is deleted, an edit is written back, a deletion is rebuilt with its child rows and, for an event, its `superseded_by` successor. A revert never touches main, needs an open branch and an editor role, and answers with a `409` — rather than a partial write — when the change cannot be undone unambiguously: two entities on the branch answer to the name and nothing records which one the entry is about (`Rename one of them, then revert.`), several rows of the branch's base snapshot answer to it with none of them named by the entry or a copy's origin (`Undo it by hand instead.`), two base events share the name of an event a restored variable override points at (`Set the overrides by hand instead.`), two events answer to the `superseded_by` successor being restored, on the branch or in the base, the parent event type is still deleted, or the branch's base snapshot predates a field the entity needs. A restored `superseded_by` whose successor no longer exists on the branch is cleared instead. A merged branch answers `409` `Branch is merged, so its plan is read-only`, and a closed one `Branch is closed — reopen it before reverting changes`.

### Updating a branch from main

When main changes after a branch is cut, the branch is *behind*: `GET /api/v1/projects/{slug}/branches/{branch_id}/conflicts` answers `behind: true`. Bring main's changes onto the branch with a three-way merge of main INTO the branch rather than recreating it:

1. `GET /api/v1/projects/{slug}/branches/{branch_id}/update-from-main` (any member, read-only) returns `behind`, `updatable`, `blockers`, `main_hash`, `main_changes` (per entity type: `added`, `changed`, `removed`, `renamed`) and `conflicts`: every field both sides changed since the base, for all six entity types, grouped per entity with `name` (the key a choice is stored under), `parent`, `label`, and per field `base`, `ours` (main), `theirs` (the branch), `choice` and `dependents`. A field of `@presence` means one side deleted what the other changed; its values are `"present"` / `"absent"`, and `dependents` counts the branch's own work under an event type that taking main's deletion would also remove.
2. `POST` the same path (editor) with `{"expected_main_hash": "<main_hash from the preview>", "resolutions": [{"entity_type", "entity_name", "field_name", "choice"}]}`. `choice` names the value to end with: `ours` takes main's, `theirs` keeps the branch's. Inline choices are stored in the same transaction. Choices saved earlier through `POST .../resolutions` count only when `expected_main_hash` is sent, because a stored choice records a side, not the values it was made against.

On success the answer is `200` with `updated`, the branch, `applied` counts and the old and new `base_revision_id`: the branch's base is now main, so its diff shows only its own work and the next merge has nothing to refuse. A branch already level with main answers `200` with `updated: false` and writes nothing. Every refusal is a `409` that writes nothing:

| `detail` | Meaning |
|---|---|
| `unresolved_conflicts` (with `conflicts`) | Some overlapping field has no choice yet. Resolve them and post again. |
| `update_blocked` | One of the preview's `blockers`: `ambiguous` (main changed a row the branch holds twice under one name, on a branch cut before origin tracking; copy your changes to a new branch) or `identity_clash` (a row of main's and one of the branch's own would share a name or `source_name`; rename the branch's one). |
| `main_moved` | Main changed after the preview that produced `expected_main_hash`. Preview again and review the new changes. |
| `incomplete_base_snapshot` | The branch predates complete merge baselines and cannot be updated. `updatable` is already `false` in the preview and in `/conflicts`. Copy your changes to a new branch. |
| `update_constraint_violation` | The database refused a uniqueness rule the preview could not foresee. |
| plain string | The branch is merged or closed. |

A successful update is audited as `plan_branch.update_from_main`.

## Search And Retrieval Flow

Start with project search when the agent has a natural-language question or a partial event name:

```http
GET /api/v1/projects/{slug}/search?q=purchase%20success&types=event&limit=10
GET /api/v1/projects/{slug}/search?q=user_id&types=variable&limit=10&branch=<branch_id>
```

Useful query parameters:

- `q`: required search text, 1 to 500 characters.
- `types`: optional repeated filter, taking the same values a result's
  `entity_type` carries. The accepted set is enumerated on the parameter itself
  in `/openapi.json` — read it from there rather than from a list here, since it
  grows as new kinds are indexed. It spans plan content and project
  configuration alike, so scan configs and alert rules are filterable values.
- `include_archived`: defaults to `false`.
- `semantic`: defaults to `true`. `false` skips the embedding leg and answers
  from the keyword index alone — much sooner, with `semantic_used` always
  `false`. The command palette asks this way first and upgrades to the full
  answer when it lands.
- `limit`: 1 to 100, defaults to 20.
- `branch`: optional branch id.
- `group_variants`: defaults to `false`. `true` folds events of one event type
  whose names differ only in one naming-rule placeholder into their best-ranked
  hit, which then carries a `variant_group`
  (`key`, `pattern`, `placeholder`, `count`, `variants[]`); the other members
  are not returned as separate results. `limit`, `total` and `truncated` then
  count rows, so a folded group is one.

Search results include `entity_type`, `entity_id`, `title`, `subtitle`,
`description`, `snippet`, `route_path`, `score`, `confidence`, and `highlights`.
Results linked to a concrete catalog event also include `event_id`, `name`, the
compatibility `implemented` projection, and safe `variable_values` contexts with
possible values for non-sensitive fields.

Use entity-specific endpoints for full context after search:

```http
GET /api/v1/projects/{slug}/events/{event_id}?branch=<branch_id>
GET /api/v1/projects/{slug}/events?search=purchase&limit=50&branch=<branch_id>
GET /api/v1/projects/{slug}/event-types
GET /api/v1/projects/{slug}/event-types/{event_type_id}
GET /api/v1/projects/{slug}/event-types/{event_type_id}/fields
GET /api/v1/projects/{slug}/variables?limit=200&offset=0&branch=<branch_id>
GET /api/v1/projects/{slug}/variables/{variable_id}/values?branch=<branch_id>
GET /api/v1/projects/{slug}/variables/{variable_id}/event-overrides?branch=<branch_id>
GET /api/v1/projects/{slug}/variables/drifts?branch=<branch_id>
```

`GET /projects/{slug}/events/{event_id}` and its `/history` answer for an event
on **any** branch of the project, whatever `branch` you pass or omit — a link
handed over with a branch id resolves without first looking the branch up — and
the response's `branch_id` says which branch the row belongs to. Writes stay
strict: a `PATCH` must name the event's own branch.

Event responses include:

- event identity and state: `name`, the free-text `title`, `description`,
  lifecycle `status`, `reviewed`, `owner_id`, optional `sunset_at`, and
  `branch_id`;
- event type id and brief event type data;
- field values and meta values;
- tags;
- metric breakdown columns;
- variable value contexts on field values that contain real `${variable}` placeholders.

`/variables` is paginated and returns `{"items": [...], "total": <int>}`.
`offset` defaults to `0` (minimum `0`) and `limit` defaults to `200` (`1` to
`5000`); out-of-range or non-numeric values are rejected with `422`. Read `total`
to decide whether another page is needed rather than assuming one response holds
the whole catalog.

`usage=all|used|unused` narrows the listing: `unused` returns exactly the rows a
retirement pass would take, `used` its complement. It is answered by the same
retirement predicate rather than by a "zero usage count" shortcut, so `unused`
never offers up a variable that a live event value still names. The default is
`all` and an unrecognised value is a `422`. `total` reflects the filter, so it
stays the honest count for whichever set you asked for.

Each item in `items` includes `allowed_values`, warehouse/JSON-path `bindings`,
`excluded_from_scans`, usage summaries, `open_drift_count`, and two inline
previews that spare a per-variable follow-up call: `sample_values` (observed
values unioned across every context, de-duplicated, capped at 20) and
`event_names` (distinct names of the events the variable was observed in,
alphabetical, capped at 20 — `event_count` carries the untruncated total).

`/variables/{variable_id}/values` returns the full per-event observed contexts
for one variable: low-cardinality contexts list all observed values, while
high-cardinality contexts list bounded samples and an observed count. A context
over a plain column takes its kind and its count from a `COUNT(DISTINCT)` over
the scanned window, but one over a JSON-path binding is always high-cardinality
and counts only what a capped sample turned up — report "at least N", never N.
Reach for it only when the inline previews are not enough. Event overrides
replace the global documented list for their event.

The catalog is not append-only. A catalog scan run can retire the scan-created
variables nothing refers to any more — no `${token}` in any stored event field
or meta value, no observed context, no value drift, no per-event override — so a
variable id cached from an earlier read can be gone by the next call. A scan
started by hand always retires; a scheduled collection retires too, judging a
variable minted from a path inside a JSON column on every run and one minted
from a scalar column only when the config declares a lookback window, because
one quiet interval can flip a scalar column to literals in every event at once
and a run must not recycle the variable on that evidence; a replay never. A
variable your agent edited, documented, bound, or excluded from scans is never
retired, and so is one renamed to anything the scan would not have chosen for
that path itself.
The branch-wide version of the same pass,
`POST /projects/{slug}/danger/retire-unused-variables`, is not available to
agents: it takes the strict owner gate and rejects every API key.

## Updating Events

Agents that only read should use a `read` key. Agents that edit need a `write` key backed by an editor or owner user.

Patch one event:

```http
PATCH /api/v1/projects/{slug}/events/{event_id}?branch=<branch_id>
Content-Type: application/json
Authorization: Bearer tk_...
```

Example payload for a description-only update:

```json
{
  "description": "Fired after checkout succeeds and the order id is available."
}
```

Example payload for state-only review workflow:

```json
{
  "reviewed": false,
  "status": "in_review"
}
```

`EventUpdate` fields are optional and partial:

- `name`
- `title` — a free-text label (max 500), shown beside the name and searchable,
  never part of the scan identity; `EventCreate` takes the same field,
  defaulting to `""`
- `description`
- `status`
- `sunset_at`
- `owner_id`
- `reviewed`
- `metric_breakdown_columns`
- `tags`
- `field_values`
- `meta_values`

When updating `field_values` or `meta_values`, send the full replacement list
for that collection. For narrow text edits, prefer patching only `description`,
`title`, `name`, tags, or state fields — and where a scan names the type, fix a
wrong label through `title`, since `name` is the identity. Values written
through event mutations are treated as authored and are protected from later
scan overwrite; re-sending an unchanged value keeps its flag as it was.

On every partial-update body in the API — events, event types, fields, meta
fields, scan configs, data sources, variables and projects — omitting a field is
how you leave it alone, and sending it as an explicit `null` means "clear it".
A `null` on a field whose column cannot be empty is refused with a `422` naming
the field (`Field(s) cannot be null: status`). On `EventUpdate` those are `name`,
`description`, `status` and `reviewed`; `sunset_at`, `owner_id` and
`superseded_by_event_id` all accept a `null` and clear, `title` reads a `null` as
`""`, `metric_breakdown_columns` reads one as `[]`, and `tags`, `field_values`
and `meta_values` read one as "leave the children alone". These requests all
failed before; only the status code and the message changed.

Every `meta_field_definition_id` in a patch, and the `event_type_id` in a create,
must come from a listing read with the same `branch` you are writing to. A branch
holds its own copy of every event type and meta field under a new id, so an id
read without `branch` is `main`'s and is refused with a `422` on a branch write.
Because `meta_values` is a full-list replacement, you cannot get past that `422`
by dropping the offending entry without losing the event's other meta values —
re-read the meta fields on the right branch instead. Tags are
stored lower-cased, trimmed and de-duplicated, and one over 100 characters is a
`422`; a meta value over 2,000 bytes as stored is a `422` too (for a field with
a link template, only the part the template wraps is stored).

Event create and patch return `EventMutationResponse`, which is the event plus a
`warnings` array. When a scan config governs the event type with an
`event_name_format`, manual creation derives the canonical name from the
referenced field values. Missing template values produce `422`; a derived name
another event of the type already holds produces `409` naming that event — the
scan identity is a unique key in the database, so two creates racing for one
name end the same way, the loser with that `409` and never a second event, and
`POST /projects/{slug}/events/bulk` prefixes the same message with
`Event N of M: `; a differing client-supplied name is ignored with a warning.
Read the mutation response and use its returned name/id instead of assuming
your proposed name became the identity. The resolved rule is on the event type
itself — `event_name_format` on `GET /event-types` and
`GET /event-types/{event_type_id}`, `null` when no scan names the type — and it
governs a branch copy of the type exactly as it governs `main`, so read it there
rather than re-deriving it from the scan config list.

Bulk state updates are available for review/archive workflows:

```http
POST /api/v1/projects/{slug}/events/bulk-update?branch=<branch_id>
```

Payload:

```json
{
  "event_ids": ["00000000-0000-0000-0000-000000000000"],
  "reviewed": true,
  "status": "ready_for_dev"
}
```

The uniform bulk patch supports `status`, `sunset_at`, `owner_id`, and
`reviewed`. Bulk delete is a separate endpoint; both are write operations.

Which fields you **send** is what the request means, not what values they hold.
A field you leave out is left alone across the whole selection. An explicit
`null` for `sunset_at` or `owner_id` clears that field across the whole
selection — `{"event_ids": [...], "owner_id": null}` is how you unassign a
selection, and it is the only way to do it. `status` and `reviewed` are NOT NULL
columns: an explicit `null` for either is refused with 422. A body that sends
nothing but `event_ids` is refused with 422 as well. The web UI spells the same
unassign as an **Unassign** entry in the bulk bar's owner picker.

## Search Indexing

The API reindexes the affected branch after normal plan mutations. Agents usually do not need to call reindex manually.

Manual reindex is editor-only:

```http
POST /api/v1/projects/{slug}/search/reindex?branch=<branch_id>
```

Use this after out-of-band maintenance or imports if search results look stale. When embeddings are enabled, the normal embedding refresh flow is scheduled by the backend.

## Dry-Running a Scan

Ask what a scan config *would* create, without writing anything:

```http
POST /api/v1/projects/{slug}/scans/dry-run
```

Send either a saved config:

```json
{ "scan_config_id": "…", "sample_row_limit": 5000 }
```

or a draft, in which case `data_source_id` and `base_query` are both required and
every other field is optional (`event_type_id`, `event_type_column`,
`time_column`, `event_name_format`, `event_group_rules`, `json_value_paths`,
`cardinality_threshold`, `app_version_column`, `platform_column`,
`scan_lookback_hours`). When `scan_config_id` is present the draft fields are
ignored.

It answers `202` with a job record; poll it:

```http
GET /api/v1/projects/{slug}/scans/dry-run-jobs/{job_id}
```

Same 202-and-poll shape as `/scans/preview`, and for the same reason: a dry run
issues the same `GROUP BY ALL` a real scan issues, which can outlive a gateway
timeout. While `status` is `pending` or `running`, `result_summary` is `null`.
On `completed` it holds:

```json
{
  "window_from": "2026-08-07T12:00:00Z",
  "window_to": "2026-08-08T12:00:00Z",
  "sampled_rows": 4812,
  "sample_row_limit": 5000,
  "sample_is_complete": false,
  "breakdown_combinations": 143,
  "events": [
    {
      "name": "Purchase Completed",
      "source_name": "Purchase Completed",
      "event_type": "Purchase",
      "approx_row_count": 3120,
      "share_of_sample": 0.648,
      "status": "new",
      "grouped_by_rule": null,
      "count_confidence": "sampled"
    }
  ],
  "events_truncated": true,
  "max_events_reached": false,
  "fields": [{ "name": "props", "type": "json", "status": "new", "event_type": "Purchase" }],
  "templated_columns": [{ "column": "country", "distinct_values": 214, "threshold": 100 }],
  "reserved_columns": ["ts", "app_version"],
  "unmapped_columns": ["legacy_flag"],
  "warnings": [],
  "errors": []
}
```

An event is identified by `event_type` **and** `source_name`, never by the name
alone: a run writes one event per event type, so a grouped scan
(`event_type_column`) whose name format collapses to the same string under two
event types produces two entries here — and `status` is resolved against that
event type's plan, not against a union.

Read it honestly. `sample_is_complete: false` means more distinct events exist
than the pass examined, so report "at least N", never N. `count_confidence` is
`"exact"` only when the sample is complete *and* no lookback window applied.
`share_of_sample` is deliberately offered instead of a projected table-wide
total — do not compute one. `errors` carries event-name-format failures verbatim
and does **not** fail the job; a non-empty `errors` means the config would fail
every real run.

Both routes are **owner-only** and session-only (an API key cannot reach them),
because the draft's `base_query` is free-text SQL run against a stored warehouse
credential. This is the same gate `/scans/preview` carries.

## Replaying Metrics

Recollect an existing scan config's metrics over a window you name:

```http
POST /api/v1/projects/{slug}/scans/{scan_id}/metrics/replay
```

```json
{
  "time_from": "2026-04-01T00:00:00Z",
  "time_to": "2026-04-02T00:00:00Z"
}
```

It answers `201` with the queued `ScanJob`; poll
`GET /api/v1/projects/{slug}/scans/{scan_id}/jobs` for its status. Use it to
backfill a window the scheduler missed or to recompute after a metric definition
changed. The config must already carry `time_column` and `interval`, otherwise
the call is `400`.

`time_to` must land **at or before the last completed interval**. The interval
still filling holds no complete bucket to replay, so a period reaching into it is
now a `400` — *"Replay period must end at or before … UTC"*, naming the latest
end it would accept — where it previously answered `201` and then produced a
failed run. An agent that posts a window ending at "now" must floor that end to
the config's own interval first.

This is the **only** owner-gated route an API key can reach, and the gate is
strict about all three of its parts: the key's scope must be `write`, the user
behind it must have the `owner` role, and a project-bound key still only reaches
its own project. An editor's `write` key gets `403 Owner role required`; a `read`
key gets `403 API key has read-only scope`.

It is reachable because a replay only re-runs SQL an owner already authored
through the browser-only scan routes — it cannot introduce a new query. Creating
or editing a scan config, like connecting a data source, stays an interactive
owner session.

## Chart annotations

Annotations are the markers charts draw at a point in time. A deploy pipeline
posts one per release so the next anomaly on a chart sits next to the deploy
that probably caused it:

```http
POST /api/v1/projects/{slug}/annotations
```

```json
{
  "label": "Deployed web 2026.09.25",
  "bucket": "2026-09-25T14:02:00Z",
  "source": "api",
  "url": "https://github.com/acme/web/releases/tag/2026.09.25"
}
```

| Field | Meaning |
|-------|---------|
| `label` | Required, 1–200 characters. |
| `bucket` | Required. When it happened, as an ISO 8601 timestamp. |
| `source` | `manual` (the default, what the app's own form sends) or `api`. `release` is reserved for the markers the metrics worker draws itself and answers `422` from a client. |
| `url` | Optional link the chart tooltip opens: `http` or `https` only, at most 500 characters. |
| `description` | Optional, up to 2000 characters. |
| `color` | Optional. Pipeline and release markers draw muted whatever colour they carry. |
| `scope_type`, `scope_ref` | Optional, and both or neither: `project_total`, `event_type`, `event` or `metric`, plus the id it names. Omit both for a project-level marker, which every monitoring (Volume tab) chart in the project shows. |

The response is the annotation, with `source` and `url` echoed back.

**Authentication.** The route is editor-level: a `write` API key backed by an
editor or owner, like every other mutation. Bind the key to the project with
`project_slug` so a leaked CI secret can annotate one project and nothing else.

**De-duplication.** Only `source: "api"` is de-duplicated on this route: the
same `(project, label)` posted with source `api` within the last 24 hours is not
created again, and the API answers **`200`** with the existing
annotation instead of **`201`** with a new one. A retried deploy job therefore
draws one marker, not two — so check the status code, not just the body, if you
need to know which happened. Put the version or commit in the label when two
deploys in a day are two separate events. `release` markers are unique per
label per project for good: one **Release *version*** marker, ever. `manual`
annotations (the default `source`) are **never** de-duplicated — every manual
create answers `201` with a new row, so a CI job that wants retry-safety must
send `"source": "api"`.

The same request from the command line is
[`tripl annotate`](../run/cli.md#tripl-annotate), which prints which of the
two answers it got. A GitHub Actions deploy step, with `curl`:

```yaml
- name: Mark the deploy on tripl charts
  run: |
    curl -fsS -X POST "https://tripl.example.com/api/v1/projects/prod/annotations" \
      -H "Authorization: Bearer ${{ secrets.TRIPL_WRITE_KEY }}" \
      -H "Content-Type: application/json" \
      -d "{\"label\": \"Deployed web ${{ github.ref_name }}\", \"bucket\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"source\": \"api\", \"url\": \"${{ github.server_url }}/${{ github.repository }}/releases/tag/${{ github.ref_name }}\"}"
```

`-f` fails the step on a `4xx`/`5xx`; a `200` for a de-duplicated label is a
success. How charts draw these markers, and the automatic **Release *version***
markers beside them, is in
[Chart annotations](../use/feature-reference.md#chart-annotations).

## Safe Agent Defaults

- Use a project-scoped `read` key for retrieval agents.
- Use a project-scoped `write` key only for agents that are explicitly allowed to edit the tracking plan.
- Pass `branch=<branch_id>` for all write calls unless the operator intentionally wants to edit main.
- Search first, then fetch the canonical entity by id before making decisions.
- Prefer partial `PATCH` payloads over sending whole objects.
- Treat field and meta value lists as full replacements when included in an event update.
- Monitoring outputs — signals, schema/distribution/variable-value drift, and
  app-version **release regressions** — are scan-produced. Query them through
  the endpoints in `/openapi.json`; only their explicit review/action endpoints
  mutate resolution state.
- Keep `/openapi.json` in the agent's tool context and use this guide for tripl-specific auth, branch, and workflow rules.

## Interactive API reference

Every endpoint — with request/response schemas — is rendered from the live
OpenAPI spec at **[API Reference](/integrate/api)** (also linked as **API** in the
top navigation). Regenerate the underlying spec with `bin/dump-openapi.sh` after
changing the HTTP API.
