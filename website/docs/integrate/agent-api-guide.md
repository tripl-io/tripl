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

If a Bearer token is invalid, expired, or revoked, the API returns `401`. If a valid key lacks scope or role permission, the API returns `403`. A project-bound key used on another project's slug gets `404 Project not found`, the same answer as a slug that does not exist, even when the key's user is a member of that project; instance-wide routes still answer `403` to a project-bound key.

Project membership:

- A key acts as the user who created it, so it reaches only the projects that
  user is a **member** of (an instance owner's key reaches every project). On any
  other project every `/projects/{slug}/...` route answers `404`
  `Project not found`, the same answer as for a slug that does not exist, and the
  project is missing from `GET /api/v1/projects` and `GET /api/v1/activity`.
- Creating a key with `project_slug` for a project the user is not a member of
  answers `404`.
- Writing needs an **editor** membership. A viewer member's key gets `403` on
  mutation routes, whatever its scope.
- A new user is a member of no project. Ask the project's creator or an owner to
  add the account behind your key.

### Project members

Read who can see a project (any member may call it):

```http
GET /api/v1/projects/{slug}/members
```

```json
[
  {
    "user_id": "7c9e…",
    "name": "Ada",
    "email": "ada@example.com",
    "role": "editor",
    "added_at": "2026-09-27T09:00:00Z"
  }
]
```

`role` is the membership role, `editor` or `viewer`. The project response
(`GET /api/v1/projects/{slug}`) also carries `my_role` (`owner`, `editor` or
`viewer`), the caller's effective role, and `can_mutate`.

Changing membership is limited to the instance owner and the project's creator,
and needs a browser session: every API key, whatever its scope, gets `403` on
these routes. The creator also needs an editing role: a creator whose instance
role is `viewer`, or who is a `viewer` member, gets `403`, and a creator who was
removed from the project gets `404`. The same applies to renaming and resetting
the project; deleting it is owner-only.

```http
POST   /api/v1/projects/{slug}/members            {"user_id": "…", "role": "viewer"}
PATCH  /api/v1/projects/{slug}/members/{user_id}  {"role": "editor"}
DELETE /api/v1/projects/{slug}/members/{user_id}
```

Adding someone who is already a member answers `409`; an unknown user or
membership answers `404`. When you add an event-type owner
(`POST /api/v1/projects/{slug}/event-types/{event_type_id}/owners`) or a branch
reviewer (`POST /api/v1/projects/{slug}/branches/{branch_id}/reviewers`), the
user must be a member of the project, or the call answers `422`
`User is not a member of this project`. Removing a member also removes their
event-type ownerships and pending branch-reviewer assignments in that project,
and closes a live-updates stream they have open within one heartbeat.

One existence signal is unavoidable: slugs are unique across the instance, so
`POST /api/v1/projects` or a rename to a slug that is already taken answers
`409` even when the caller cannot see the project holding it.

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
  lifecycle `status`, `reviewed`, `owner_id`, optional `sunset_at`,
  `superseded_by_event_id`, `first_seen_at` (when a scan first saw the event
  with volume; `null` if never), and `branch_id`;
- `lifecycle_findings`: the event's sunset-watch findings (see
  [Event lifecycle](#event-lifecycle));
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

## Source freshness

Before reading a drop as real, check whether the source behind it is simply
late. Freshness for every scan config in a project is one call:

```http
GET /api/v1/projects/{slug}/source-freshness
```

It returns one item per scan config, with `id`, `name`, `data_source_id` and
`freshness`. The same `freshness` object is on every `ScanConfig` response
(`GET /api/v1/projects/{slug}/scans` and a single scan), so an agent that
already holds a scan config does not need the second call.

| `freshness` field | Meaning |
|-------|---------|
| `status` | `fresh`, `late`, `overdue` or `unknown`. |
| `lag_seconds` | Seconds between now and `last_event_at`. `null` when no event has been observed. |
| `last_event_at` | The start of the newest bucket that had events in the latest successful metrics collection. It has bucket resolution, so it can sit up to one interval behind the newest event. `null` before the first collection. |
| `last_collection_at` | When that collection completed. `null` before the first collection. |
| `expected_by` | The moment after which the source counts as `late`. `null` when the status is `unknown`. |

The status is computed each time the response is built, and is never stored:

- `overdue`: `now − last_collection_at > 2 × interval`. The scan is not
  running on schedule. This takes precedence over `late`.
- `late`: `now − last_event_at` reaches
  `min(3 × interval, settling allowance rounded up to whole intervals + 2 × interval)`,
  where the settling allowance is the project's
  `anomaly_ingestion_settling_minutes`.
- `unknown`: the scan has no interval (a manual scan), or nothing has been
  collected yet.
- `fresh`: otherwise.

While a scan is `late` or `overdue`, the metrics worker holds that scan's
drop-direction volume signals instead of emitting them. A missing drop on a
late source therefore means "not yet judged", not "no drop". The scan job's
`result_summary` reports this as `freshness_status` and `signals_held`. Once
data lands, the next run re-collects the buckets that were empty during the
delay before scoring them. A replay only refreshes `last_event_at` when it
reaches past it. See
[Drop signals are held while a source is late](../use/anomaly-detection.md#held-while-late)
and the [Source freshness](../use/alerting.md#source-freshness) alert scope,
which rules enable with `include_source_freshness`. A `late` alert comes from
the scan's own collection run; an `overdue` alert comes from a sweep that runs
every 15 minutes. One delay or outage is one alert.

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

## Signal verdicts {#signal-verdicts}

A verdict records what a monitoring signal turned out to be. Set one with an
editor-level `write` key:

```http
POST /api/v1/projects/{slug}/signals/verdict
```

```json
{
  "scope_type": "event",
  "scope_ref": "d4c684dd-…",
  "scan_config_id": "5b1e…",
  "bucket": "2026-09-25T18:00:00Z",
  "verdict": "tracking_bug",
  "note": "Fires twice on Android 7.4.0"
}
```

| Field | Meaning |
|-------|---------|
| `scope_type`, `scope_ref`, `scan_config_id`, `bucket` | The signal's key, as on the signal itself. `scan_config_id` is `null` for a catalog metric. |
| `verdict` | `expected`, `tracking_bug`, `false_positive` or `real_issue`. |
| `expected_reason` | With `expected` only: `campaign`, `release`, `seasonality` or `other`. |
| `note` | Optional free text. The author and time are recorded from the request. |

`DELETE` on the same path with the same key clears the verdict. What each one
does: `expected` writes an annotation on the bucket and hides that one signal
(it does not suppress later buckets); `false_positive` tunes the scope's
detection thresholds exactly like an incident false positive. When the signal
belongs to an incident, the request updates the incident instead —
`false_positive` → `false_positive`, `real_issue` and `tracking_bug` →
`acknowledged`, `expected` → `resolved`. See
[Verdicts](../use/anomaly-detection.md#signal-verdicts).

Signal payloads carry the result as `verdict` —
`{verdict, expected_reason, note, author_name, created_at, source}`, where
`source` is `signal` or `incident` — and `incident` — `{id, status}` — each
`null` when absent. To list only undecided signals, add `needs_verdict=true` to
`GET /api/v1/projects/{slug}/anomalies/signals`.

Per-project totals, for example for a health score, are one read-level call:

```http
GET /api/v1/projects/{slug}/signals/verdict-counts
```

```json
{
  "needs_verdict": 4,
  "expected": 2,
  "tracking_bug": 1,
  "false_positive": 3,
  "real_issue": 0
}
```

`needs_verdict` counts the signals without a verdict; the other four count
signals by the verdict they carry. An acknowledged signal has no verdict and
counts under `needs_verdict`.

## Owner notifications {#owner-notifications}

A rule can email the owners of what it matched, in addition to its destination.
The switch is `notify_owners` (boolean, default `false`) on the alert rule —
accepted by create
(`POST /api/v1/projects/{slug}/alert-destinations/{destination_id}/rules`) and
update (`PATCH …/rules/{rule_id}`), and returned on every rule response.

Owners of a matched item are the event type's owners on `main` (for an event,
its type's owners; for an event type, its own; for any other scope about an
event or event type — drift, release regression, lifecycle — that type's
owners) plus, for a catalog metric, the metric's `owner_id`; project total and
source freshness have none. Only current project members with an account
email are emailed; an owner who is not a member or has no email is neither
notified nor listed. Each owner gets one plain-text email per rule delivery,
sent after the rule's delivery is sent, through the instance SMTP settings. The
email uses the default item lines (the digest's lines for a digest), not the
rule's custom template. A digest that batches several rules sends one email
per rule delivery, so an owner of items in two of those rules can get two
emails.

The delivery detail (`GET /api/v1/projects/{slug}/alert-deliveries/{delivery_id}`)
carries the result as `owner_notifications`; the list route does not include
it:

```json
[
  {"user_id": "8f0c…", "name": "Anna", "email": "anna@example.com", "status": "sent", "error": null, "sent_at": "2026-09-25T18:04:11Z"},
  {"user_id": "12ab…", "name": "Oleg", "email": "oleg@example.com", "status": "skipped", "error": "SMTP is not configured; owner email skipped.", "sent_at": null}
]
```

`status` is one of:

- `sent` — the email went out;
- `failed` — the mail server rejected it (see `error`); other owners are still
  attempted;
- `skipped` — email was unavailable: no SMTP server or Default From address,
  or a demo project;
- `pending` — a short-lived in-progress claim while the send runs; a claim
  older than 15 minutes is treated as abandoned and reclaimed.

The list is empty when the rule does not notify owners or nothing matched had
an owner. A `sent` owner is never emailed twice for the same delivery; a
retried or re-run delivery re-attempts `skipped` and `failed` owners (and stale
`pending` ones).

Two editor-level routes send a one-off email to the current owners:

```http
POST /api/v1/projects/{slug}/alert-inbox/{correlation_group_id}/notify-owners
POST /api/v1/projects/{slug}/signals/notify-owners
```

The first targets an incident. The second targets a signal — including one no
rule routed — and takes the signal key used by
[signal verdicts](#signal-verdicts):

```json
{
  "scope_type": "event_type",
  "scope_ref": "3a9e…",
  "scan_config_id": "5b1e…",
  "bucket": "2026-09-25T18:00:00Z"
}
```

Both return the owners they considered, with the same fields as above:

```json
{
  "owners": [
    {"user_id": "8f0c…", "name": "Anna", "email": "anna@example.com", "status": "sent", "error": null, "sent_at": "2026-09-27T09:12:40Z"},
    {"user_id": "12ab…", "name": "Oleg", "email": "oleg@example.com", "status": "skipped", "error": "notified 4 minutes ago", "sent_at": null}
  ]
}
```

A manual notify has a 10-minute cooldown per incident (or signal key) and
owner: an owner notified by hand within the last 10 minutes is returned as
`skipped` with an error such as `notified 4 minutes ago`. One request notifies
at most 20 owners.

Neither route depends on the rule's `notify_owners` setting. See
[Notifying owners](../use/alerting.md#owner-notifications).

## Signal attribution {#signal-attribution}

Attribution says where a volume signal's change came from — which breakdown
column values explain the delta, and whether a release rolled out just before
it. It is computed when the anomaly is detected and stored with it, so every
surface returns the same numbers and the same sentences. See
[Why it changed: attribution](../use/anomaly-detection.md#attribution) for the
math.

Signal payloads — the signals list and a drilldown's `latest_signal` — carry
three fields. Chart anomaly points do **not** carry attribution; a chart point
that needs it goes through its signal, or through the per-anomaly route below.

| Field | Meaning |
|-------|---------|
| `anomaly_id` | The stored anomaly behind the signal — the id the per-anomaly route takes. |
| `attribution_status` | `ready`, `no_breakdown_columns` or `not_computed`. |
| `attribution` | The stored attribution when the status is `ready`, otherwise `null`. |

```json
{
  "attribution_status": "ready",
  "attribution": {
    "delta": -3390,
    "columns": [
      {
        "column": "platform",
        "explained_share": 0.92,
        "values": [
          {"value": "ios", "delta": -3120, "expected": 3400, "actual": 280, "share": 0.92},
          {"value": "web", "delta": 40, "expected": 900, "actual": 940, "share": -0.012}
        ]
      }
    ],
    "release": {
      "version": "4.12",
      "previous_version": "4.11",
      "share": 0.38,
      "reached_at": "2026-09-25T15:00:00Z"
    },
    "headline": "92% of the drop comes from platform = ios (−3,120 of −3,390)",
    "release_line": "Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop",
    "computed_at": "2026-09-25T18:04:11Z"
  }
}
```

| Field | Meaning |
|-------|---------|
| `delta` | The scope's actual minus expected in the flagged bucket. |
| `columns` | Up to 3 breakdown columns. `explained_share` (0–1) is the part of `delta` the column's same-direction top values explain. |
| `columns[].values` | Up to 3 named values per column: `expected`, `actual`, their difference `delta` (the value's contribution), and `share` — that contribution over the scope's `delta`, **signed**: a value that moved against the change has a negative share. The remainder outside the listed values is booked to *Other*, which is never listed as a value, so a column's contributions plus *Other* sum to `delta`. |
| `release` | `{version, previous_version, share, reached_at}` when a new app version crossed the release gate shortly before the bucket, else `null`. `previous_version` is `null` when no earlier released version carried the traffic. App-version series feed only this field, never `columns`. |
| `headline` | The one-sentence summary, or `null` when there is nothing to say. It is exactly the sentence the drilldown's **Why** panel and the [alert line](../use/alerting.md#attribution-line) print — quote it rather than rebuilding it from `columns`. |
| `release_line` | The release sentence, or `null` when `release` is `null`. Same rule: quote it verbatim. |
| `computed_at` | When the metrics worker stored this attribution. |

`headline` takes one of two forms:

- `<N>% of the <drop|spike> comes from <column> = <value> (<value delta> of <scope delta>)`
  — the column with the highest `explained_share`, and within it the largest
  value moving the same way as the delta. Counts use thousands separators and a
  real minus sign (`−`).
- `<Column> shifted in both directions; no single value explains the <drop|spike>`
  — with no percent, when that column's values moved in both directions by more
  than twice the delta in total, or none of them moved the same way as the
  delta.

`release_line` reads `Release <version> (after <previous>) reached <N>% of traffic <H>h before the <drop|spike>`,
with the hours rounded down, `at the <drop|spike>` in place of the lead time
when the release activated in the flagged bucket itself, and no `(after …)`
when `previous_version` is `null`.

To load it on its own, for example lazily on a detail view or for a chart point,
use a read-level key:

```http
GET /api/v1/projects/{slug}/anomalies/{anomaly_id}/attribution
```

```json
{
  "anomaly_id": "7c0e…",
  "scan_config_id": "2b9f…",
  "attribution_status": "ready",
  "attribution": {"delta": -3390, "columns": ["…same shape as above…"], "headline": "92% of the drop comes from platform = ios (−3,120 of −3,390)"}
}
```

It returns the anomaly's id, the scan it belongs to (`null` for catalog-metric
anomalies), and the same `attribution_status` and `attribution` a signal
carries. Attribution is read-only: replaying a period recomputes it, and it is
dropped with its anomaly.

## Dependencies and impact {#dependencies-and-impact}

Before an agent deletes, deprecates or renames a plan entity, it can ask what
depends on it. All three routes are read-only, need only a `read` key, and
follow the project's membership like every other `/projects/{slug}` route. None
of them changes what a delete, deprecate or rename does: those writes still
succeed with dependents in place. Fact tables keep their existing `409` when a
delete or edit would break a metric; nothing else gains one. See
[Dependencies & impact](../use/dependencies-and-impact.md) for which edges exist.

Look up one entity's dependencies:

```http
GET /api/v1/projects/{slug}/dependencies?entity=event:5a1f…&depth=1
```

`entity` is `<kind>:<id>`, with `kind` one of `event`, `event_type`, `field`,
`variable`, `metric`, `fact_table`, `alert_rule` or `relation`. `depth` is `1`
(the default, direct neighbours only) or `2` (one more hop, for example the
alert rules scoped to the metrics that use an event). Plan entities are resolved
on the branch named by the usual `branch` query parameter, or on main without
one; pass the branch copy's own id. An id that resolves to nothing is not an
error: the response is `200` with `entity.exists` set to `false`, `name` null,
and whatever project-wide rows still name that id.

```json
{
  "entity": { "kind": "event", "id": "5a1f…", "name": "checkout:completed",
              "exists": true },
  "upstream": [
    { "kind": "event_type", "id": "91c0…", "name": "checkout",
      "relation": "event belongs to event type", "certainty": "direct",
      "url_hint": "/p/shop/event-types/91c0…", "depth": 1 }
  ],
  "downstream": [
    { "kind": "metric", "id": "c3d2…", "name": "Checkout conversion",
      "relation": "metric uses event in its composition", "certainty": "direct",
      "url_hint": "/p/shop/monitoring/metric/c3d2…", "depth": 1 },
    { "kind": "alert_rule", "id": "0b7e…", "name": "Checkout volume",
      "relation": "alert rule filters on event", "certainty": "direct",
      "url_hint": "/p/shop/alerting", "depth": 1 },
    { "kind": "fact_table", "id": "e410…", "name": "orders",
      "relation": "fact table SQL or columns mention the column by name",
      "certainty": "possible",
      "url_hint": "/p/shop/metrics/fact-tables/e410…/edit", "depth": 1 }
  ],
  "counts_by_kind": { "metric": 1, "alert_rule": 1 },
  "possible_counts_by_kind": { "fact_table": 1 }
}
```

Every edge has the same shape: the other entity's `kind`, `id` and `name`, a
`relation` sentence saying why the edge exists, a `certainty`, a `url_hint` and
a `depth`. An edge's `kind` can also be `scan_config` (a scan's breakdown,
drift, platform or app-version column, or its event type binding), which has no
dependencies route of its own.

- `certainty` is `direct` for a stored reference (an id, or a column name read
  in the entity's own scope) and `possible` for a match by name without a
  stored id: an SQL identifier or JSON-key literal in a `sql` metric's query,
  filter SQL or a fact table's SQL, a fact-table or `fact` metric column, a
  variable binding by column name, a column on a scan with no event type. Treat
  `possible` as "check it", never as proof.
- `url_hint` is the entity's path in the app, without `?branch=`. It is filled
  for every kind except a field whose event type cannot be found, where it is
  `null`.
- `depth` is `1` for a neighbour of the asked entity and `2` for a neighbour of
  a neighbour (only with `depth=2`).
- `counts_by_kind` counts the direct, depth-1 downstream edges;
  `possible_counts_by_kind` counts the rest of `downstream` (possible matches
  and depth-2 edges).

See [Dependencies & impact](../use/dependencies-and-impact.md#what-counts-as-a-dependency)
for every edge, including *superseded by* links between events, detection
overrides, variables used in field and meta values, and scan drift, platform
and app-version columns.

Ask about a set of planned changes at once:

```http
POST /api/v1/projects/{slug}/impact
```

```json
{
  "changes": [
    { "kind": "event", "id": "5a1f…", "change": "delete" },
    { "kind": "variable", "id": "77aa…", "change": "rename" }
  ]
}
```

`changes` holds 1 to 200 items; more is a `422`. A caller sends `change` as
`delete`, `deprecate` or `rename`. Every change is resolved at depth 1. The
response has one item per change:

```json
{
  "items": [
    {
      "change": { "kind": "event", "id": "5a1f…", "change": "delete" },
      "entity": { "kind": "event", "id": "5a1f…", "name": "checkout:completed",
                  "exists": true },
      "name": "checkout:completed",
      "affected": [
        { "kind": "metric", "id": "c3d2…", "name": "Checkout conversion",
          "relation": "metric uses event in its composition",
          "certainty": "direct",
          "url_hint": "/p/shop/monitoring/metric/c3d2…", "depth": 1 }
      ],
      "summary": "1 metric"
    }
  ]
}
```

`name` repeats `entity.name` for list rows. It writes nothing. Use `summary`
when you report to a person (possible matches are counted apart, as in
*1 metric, plus 1 fact table that may use it*), and `affected` when you decide
what else to update first.

Get the same answer for everything a plan branch changed:

```http
GET /api/v1/projects/{slug}/branches/{branch_id}/impact
```

The response has the same `items` shape as `POST /impact`, with the change set
taken from the branch's diff: deleted, renamed, deprecated or archived, and
otherwise edited events, event types, fields and variables. Here `change` can
also be `change`, a response-only value for an entity edited in place (a
field's type, an event's breakdown columns) without being renamed, deprecated or
archived. A rename appears once, paired the way the diff's `renames` list pairs
it; additions are left out. This is what the branch's **Impact** panel shows;
an agent reviewing a branch can read it before approving.

## Event lifecycle {#event-lifecycle}

Some lifecycle facts come from the data rather than from an edit. They are
written on `main` only and never through a branch — see
[Going live on its own](../use/feature-reference.md#going-live) and
[Sunset watch](../use/feature-reference.md#sunset-watch).

**Going live.** The first scan that sees an event with volume sets its
`first_seen_at` (once; `null` until then) and, for a `ready_for_dev` or
`implemented` event whose required fields all have a non-empty value, moves
`status` to `live`. The move is not a plan write: it bypasses branch rules, and
the event's `/history` records it as a `status` row whose `author_label` is
`tripl (scan)` and whose `user_id` / `user_email` are `null`. The label comes
from the change's recorded source (a scan), not from the missing user, so read
`author_label` rather than inferring a scan from `user_id` being `null`; a
person's edit carries `author_label: null` and names the person in
`user_email`. An event with a required field left blank is not promoted, however
much traffic arrives — this is a change from earlier releases, which promoted on
volume alone. When the event has an implementation
ticket, the ticket gets a comment saying when the event was seen. Do not try to
reproduce this by `PATCH`ing `status` to `live` yourself — an agent's edit is a
plan change and goes through the branch like any other.

**Lifecycle findings.** A daily check records open problems with retirements:

```http
GET /api/v1/projects/{slug}/lifecycle-findings
```

The response is `{"items": [...], "total": N}`, open findings only;
`?include_resolved=true` adds the closed ones. Each item:

| Field | Meaning |
|-------|---------|
| `id` | The finding's id. |
| `event_id` / `event_name` | The **deprecated** event the finding hangs on — for both kinds, including `successor_silent`. |
| `related_event_id` / `related_event_name` | For `successor_silent`: the successor that has gone quiet. `null` for `sunset_overdue`. |
| `kind` | `sunset_overdue` — a deprecated event past `sunset_at` that still had volume in the last 24 hours; `successor_silent` — the successor of a deprecated event (the event its `superseded_by_event_id` names) had no volume in the last 7 days. |
| `first_seen_at` / `last_seen_at` | The first and the latest daily check that found the condition. |
| `resolved_at` | When the condition cleared; `null` while the finding is open. |
| `volume_24h` | For `sunset_overdue`: the old event's count over the last 24 hours. |
| `successor_volume_7d` | For `successor_silent`: the successor's count over the last 7 days. |

The same findings appear on the deprecated event itself as `lifecycle_findings`
in `GET /projects/{slug}/events/{event_id}`, and each events-list item carries a
boolean `lifecycle_warning` that is true while the event has an open finding —
enough to flag it in a listing without a second call. Because every finding
hangs on the deprecated event, the flag is on the deprecated event for both
kinds; a silent successor is not itself flagged. A finding is updated in
place by each check and resolved when its condition clears, so a list of open
findings is the current state, not a log. Rules alert on open findings when
`include_lifecycle` is true — see
[Lifecycle alerts](../use/alerting.md#lifecycle).

**Successor adoption.** For a deprecated event that names a successor:

```http
GET /api/v1/projects/{slug}/events/{event_id}/migration
```

```json
{
  "old": { "event_id": "…", "name": "checkout_v1", "daily_avg_7d": 1240 },
  "new": { "event_id": "…", "name": "checkout_completed", "daily_avg_7d": 3800 },
  "ratio": 3.06
}
```

`daily_avg_7d` is each event's average daily volume over the last 7 days; a
collected bucket that straddles either edge of the window counts in proportion
to the part of it inside the window. `ratio` is new over old — how many times
the old event's volume the successor now receives (here 3,800 / 1,240 ≈ 3.06) —
and is `null` when the old event's average is `0`, where no ratio exists. Use it to judge whether a retired event can be
archived: a successor well ahead of the old event and an old event near zero is
a migration that has landed. The route answers only for a deprecated event with
a successor.

**Implementation tracker.** `GET`/`PATCH /api/v1/projects/{slug}/tracker-config`
(owner-only for writes) takes `tracker_type` `jira` or `linear`. Linear needs a
`team_id` and the API key; as with Jira's token the key is write-only, encrypted
at rest, and reported back only as whether one is set. Switching
`tracker_type` between `jira` and `linear` clears the stored credential (and the
Jira project key), since a Jira token must never be sent to Linear or the other
way round: send the new tracker's token or key in the same `PATCH`, or the
tracker is left without one. Tickets are read the same
way for both trackers, from the branch and event `implementation-tickets` routes.

## Notifications and subscriptions {#notifications}

Every user has an in-app notification list, email preferences, and a set of
subscriptions (watches) on events, event types, metrics and branches. A key
acts as the user who created it, so these routes read and change **that
user's** notifications and subscriptions. The product behaviour is described
in [Notifications & watching](../use/notifications.md).

### Reading notifications

```http
GET /api/v1/me/notifications?unread=true&limit=30&cursor=…
GET /api/v1/me/notifications/unread-count
```

The list covers every project the user is currently a member of, newest
first. `unread=true` returns only unread notifications; `limit` is the page
size (default 30, at most 100). The list comes back as a page:

```json
{"items": [ … ], "next_cursor": "…"}
```

Pass `next_cursor` back as `cursor` to get the next (older) page; it is
`null` on the last page. Treat the cursor as opaque. Each item carries:

| Field | Meaning |
|-------|---------|
| `id` | notification id |
| `project_id`, `project_slug`, `project_name` | the project it belongs to |
| `kind` | `comment`, `reply`, `mention`, `open_question`, `signal`, `branch_review_requested`, `branch_approved`, `branch_merged` or `lifecycle` |
| `entity_type`, `entity_id` | what it is about: `event`, `event_type`, `metric` or `branch`, and its id |
| `title`, `body` | a short title and text |
| `url` | the in-app path to open |
| `actor` | who caused it, as `{"id", "name", "email"}`, or `null` (for example a signal, or a deleted user) |
| `read_at` | when it was marked read, or `null` |
| `created_at` | when it was created |

`unread-count` returns `{"unread": 3}`.

Mark notifications read, either by id (up to 500 per call) or all at once;
the body must name one or the other:

```http
POST /api/v1/me/notifications/read
```

```json
{"ids": ["3f2a…", "9b41…"]}
```

```json
{"all": true}
```

The response reports how many notifications changed and the unread count
left: `{"updated": 2, "unread": 1}`.

### Email preferences

```http
GET   /api/v1/me/notification-prefs
PATCH /api/v1/me/notification-prefs
```

```json
{"email_mode": "weekly", "mentions_email": true}
```

`email_mode` is `off`, `instant`, `daily` (the default) or `weekly`;
`mentions_email` (default `true`) controls email for mentions separately from
`email_mode`. Both fields are optional in a `PATCH`. The response also carries
`email_available`, which is `false` when the instance has no SMTP configured:
the preferences are kept, but nothing is emailed until SMTP is set up, and the
in-app list is unaffected. Instant emails go out within about a minute. Daily
and weekly digests group the notifications that are unread and not yet
emailed, and each notification is emailed at most once.

### Subscriptions

A subscription is per user and entity, inside a project:

```http
GET    /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
PUT    /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
DELETE /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
PATCH  /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
```

`entity_type` is `event`, `event_type`, `metric` or `branch`. `GET` returns the
caller's own state for that entity; `PUT` watches it (adds the `manual`
reason); `DELETE` unwatches it; `PATCH` sets `muted`:

```json
{"muted": true}
```

All four return the same body, the caller's subscription state:

```json
{
  "entity_type": "event",
  "entity_id": "7c1e…",
  "watching": true,
  "muted": true,
  "reasons": ["author", "commenter"]
}
```

`entity_id` is the id the subscription is kept under. For an event that is the
event's discussion home, so watching an event's copy on a plan branch returns
the id of its main-plan twin. `reasons` lists why the subscription exists:
`author`, `owner`, `commenter`, `reviewer` and/or `manual`. tripl adds the
automatic reasons itself (event author on create, event type owners through the
`event_type` subscription, the first comment on an event, branch author and
reviewers).

A muted subscription stays in place: it still reads `watching: true` with
`muted: true` and keeps its reasons, but produces no notifications except
mentions, which always get through. `DELETE` removes the subscription
entirely, muted or not.

Watching an event type brings the signals, lifecycle findings and open
questions on its events, not their ordinary comments and replies. To follow an
event's discussion, watch the event itself.

### Mentions

A comment body mentions a member with `@[Name](user_id)`. Only that form
notifies; plain `@name` text does not. Take the ids from
[`GET /api/v1/projects/{slug}/members`](#project-members). A mentioned user who
is not a member of the project is skipped. A mention notifies even when the
mentioned user has muted the thread.

### Who is notified

| `kind` | Recipients |
|--------|------------|
| `comment`, `reply` on an event | the event's watchers |
| `comment`, `reply` on a branch | the branch's watchers |
| `mention` | the mentioned members |
| `open_question` | the event's author and its event type's owners |
| `signal` | watchers of the event, event type or metric; a signal on an event also reaches its event type's watchers |
| `lifecycle` | watchers of the event and of its event type |
| `branch_review_requested` | the requested reviewers |
| `branch_approved`, `branch_merged` | the branch's author, its reviewers and its watchers |

A user who already got a `mention` for a comment does not also get the
`comment` or `open_question` for it. Recipients are checked against the
project's current members when a notification is created and again before it
is emailed, and the user whose action caused it is never notified. Signal
notifications are limited to one per new signal, at most one per entity per
subscriber every 6 hours, and are never created for hidden or verdicted
signals.

## Plan validation {#plan-validation}

Check tracking calls or captured events against the plan in one batch. This is
the validator behind [`tripl check`](./tripl-check.md). Use it directly when
your own tool already knows what an event looks like, for example a test
harness that records what the app sent.

```http
POST /api/v1/projects/{slug}/plan/validate?branch=<branch_id>
```

The route is read-like. It writes nothing, and it is a `POST` only to carry the
batch. Any project member can call it, viewers included, and a `read` key is
enough. Without `branch` it validates against main. With `branch`, it validates
against that branch's plan, with the usual `400` / `404` for a malformed or
foreign id.

```json
{
  "items": [
    {
      "ref": "App/Checkout/PayButton.swift:42:9",
      "event_type": "se",
      "name": null,
      "fields": { "category": "checkout", "action": "tap", "label": "pay_button", "plan": null },
      "properties": { "source": "cart" }
    },
    {
      "ref": "payload:7",
      "event_type": null,
      "name": "promo_banner_shown",
      "fields": {},
      "properties": { "slot": "homepage" },
      "complete": true
    }
  ],
  "strict": false
}
```

The body has two top-level keys:

| Key | Meaning |
|-----|---------|
| `items` | 1 to 5,000 items. More is a `422`, so split a larger batch. |
| `strict` | Optional, default `false`. `true` adds a `dynamic_value` `info` finding for every value that is only known at runtime. `tripl check --strict` sets it. |

Each item has these keys:

| Key | Meaning |
|-----|---------|
| `ref` | Optional. Any string. It is echoed back so you can match verdicts to your inputs, for example `file:line` or a payload line number. |
| `event_type` | The plan event type **name**, or `null`. |
| `name` | The event name or identity, or `null`. |
| `fields` | Plan field name to value: a string, number, boolean or `null`. In a call-site item (`complete` false) a `null` value means *dynamic*: the value is unknown at scan time, and a dynamic value is never an error. |
| `properties` | Optional. Other keys the event carries. Each key is checked as a field name of the type, and each literal value like a field value. |
| `complete` | Optional, default `false`. `true` says this is a whole event, as sent, rather than one call site. Only then is a required field that is absent reported, and the item's values are taken as sent: they are never holes. |

How an item is resolved:

- **With `event_type` and `fields`**: the server builds the identity with the
  type's name rule (its resolved `event_name_format`, for example
  `{category}:{action}:{label}` gives `checkout:tap:pay_button`) and looks up
  the event with that identity within the type. A `null` field the rule needs
  becomes a `${key}` **hole**: `checkout:${action}:pay_button`. A type with no
  name rule uses `name` as the identity.
- **With only `name`**: it is matched against event identities and names across
  every type. `${…}` tokens in the name (from interpolation in code) are holes
  too.

A hole matches whatever the plan has in that place, and the plan's own
`${variable}` placeholders match the item's literal text (which is then checked
against the variable's documented values). The fields an item carries are
checked against the type whether or not the identity matched. When the identity
matches no planned event:

- **Fully literal**: `unknown_event`, `error`.
- **With holes and some literal text**: `unknown_event`, `warning`. Nothing
  readable matches, but the runtime value might.
- **Only holes and separators** (`${category}:${action}:${label}`): nothing is
  matched and nothing is reported. `event_id` is `null`.
- **More than 10 holes**: `too_dynamic`, `info`. The identity is not matched.

A `complete: true` item never has holes, so an unmatched identity there is
always an error.

The response has one verdict per item, in the order of the request:

```json
{
  "items": [
    {
      "ref": "App/Checkout/PayButton.swift:42:9",
      "status": "warning",
      "event_id": "5a1f…",
      "identity": "checkout:tap:pay_button",
      "findings": [
        { "code": "unknown_field", "severity": "warning", "field": "source",
          "message": "'source' is not a field of event type 'se'" }
      ]
    },
    {
      "ref": "payload:7",
      "status": "error",
      "event_id": "9c02…",
      "identity": "promo_banner_shown",
      "findings": [
        { "code": "value_not_allowed", "severity": "error", "field": "slot",
          "message": "'homepage' is not an allowed value of slot (home, cart)." },
        { "code": "missing_required_field", "severity": "error", "field": "variant",
          "message": "Required field 'variant' is missing" }
      ]
    }
  ],
  "summary": { "ok": 0, "warnings": 1, "errors": 1 }
}
```

An item's `status` is `ok`, `warning` or `error`, the worst severity among its
findings. `summary` counts items by status. The finding codes are:

| Code | Severity | When |
|------|----------|------|
| `unknown_event_type` | error | `event_type` names no event type in the plan. |
| `unknown_event` | error, or warning | No event has the built identity, or the given name. An error when the identity is fully literal, a warning when it has holes. An identity of holes alone gets no finding. |
| `deprecated_event` | warning, or error | The matched event is `deprecated` (warning) or `archived` (error). |
| `unknown_field` | warning | A `fields` or `properties` key that the event type does not define. |
| `missing_required_field` | error | Only for `complete: true`: a required field of the type is absent. |
| `value_not_allowed` | error | A literal value is outside the field's enum options, outside the documented `allowed_values` of the variable the field refers to, or fails the field's contract regex or min/max. |
| `dynamic_value` | info | Only with `"strict": true`: a field was sent as `null` or with a hole, or the identity has holes. |
| `too_dynamic` | info | The identity has more than 10 holes, too many to match. |

An `info` finding never changes an item's `status`.

`field` names the plan field a finding is about, or is `null` for findings about
the whole item. `message` is prose and may change. Select on `code`.

## Plan export {#plan-export}

Read the whole plan in a form built for generating or validating code. This is
the endpoint behind [`tripl codegen`](./codegen.md) and
[`tripl export`](../run/cli.md#tripl-export), and behind the **Export JSON
Schema** button on the Plan history page.

```http
GET /api/v1/projects/{slug}/plan/export?format=jsonschema&branch=<branch_id>
GET /api/v1/projects/{slug}/plan/export?format=codegen_model&branch=<branch_id>
```

It is a read and changes nothing. Any project member can call it, viewers
included, and a `read` key is enough. Without `branch` it exports main. With
`branch`, it exports that branch's plan, with the usual `400` / `404` for a
malformed or foreign id. `format` defaults to `jsonschema`; an unknown format
is a `422`.

Both formats leave out **archived** events and include **deprecated** ones,
flagged. Both carry the same header keys, so a generated file can say what it
was generated from:

| Key | Meaning |
|-----|---------|
| `format` | `jsonschema` or `codegen_model`, as requested. |
| `revision` | The plan revision the export reflects: the latest revision for main, the base revision for a branch. `null` when no revision was ever taken. |
| `branch` / `branch_id` | The branch the plan was read from, by name (`main` for main) and id. |
| `plan_hash` | `sha256:…` over the exported content. It changes exactly when the export would, so a CI job can compare it instead of the whole body. |

There is no timestamp: the same plan exports byte-identical.

### `format=jsonschema` {#plan-export-jsonschema}

One [JSON Schema](https://json-schema.org/draft/2020-12) document per event,
keyed `<event_type>/<identity>`:

```json
{
  "format": "jsonschema",
  "revision": "3f9c2a1e-…",
  "branch": "main",
  "branch_id": "0b7d…",
  "plan_hash": "sha256:9e41…",
  "schemas": {
    "se/checkout:tap:pay_button": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "checkout:tap:pay_button",
      "type": "object",
      "properties": {
        "category": { "type": "string", "const": "checkout" },
        "action": { "type": "string", "const": "tap" },
        "label": { "type": "string", "const": "pay_button" },
        "plan": { "type": "string", "enum": ["annual", "monthly"] },
        "coupon": { "type": "string", "pattern": "^[A-Z0-9]{6,12}$" },
        "cart_value": { "type": "number", "minimum": 0, "maximum": 100000 }
      },
      "required": ["category", "action", "label"],
      "x-tripl": {
        "event_type": "se",
        "identity": "checkout:tap:pay_button",
        "name": "checkout:tap:pay_button",
        "status": "live"
      }
    },
    "track/Home Screen View": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "Home Screen View",
      "type": "object",
      "properties": {
        "platform": { "type": "string", "enum": ["android", "ios"] },
        "source": { "type": "string" }
      },
      "required": ["platform"],
      "x-tripl": { "event_type": "track", "identity": "Home Screen View", "name": "Home Screen View", "status": "live" }
    }
  }
}
```

Two events of one type with the same identity keep both schemas; the second is
keyed `…#2`.

How a field becomes a property:

| Plan | Schema |
|------|--------|
| Field type | `string`, `enum` → `type: string`; `url` → `type: string, format: uri`; `number` → `type: number`; `boolean` → `type: boolean`; `json` → no `type` (anything). |
| Required field | Listed in `required`. |
| The event's value is a literal (`checkout`, `9.99`) | `const`, typed by the field type: a number field's `"9.99"` is the number `9.99`. |
| The event's value is a whole `${variable}` | `enum` of the variable's allowed values (the event's own override list when it has one). No allowed values: no constraint. |
| The event's value is a template (`item_${kind}`) on a string field | An anchored `pattern`, each hole an alternation of the allowed values, or `.*`. |
| Enum field | `enum` of its options. |
| Contract regex | `pattern`. Unanchored: the same partial match `tripl check` and the drift job apply. |
| Contract min, max | `minimum`, `maximum`, on number fields only. |

When the event's value and a contract both set the same keyword (for example a
`${variable}` `enum` on an enum field), the contract's copy goes into `allOf`,
so both hold. A field's
display name and description become its `title` and `description`, an event's
description becomes the schema's `description`, and a deprecated event has
`"deprecated": true`. The `x-tripl` object is an annotation (event type,
identity, name, status); a 2020-12 validator ignores it.

### `format=codegen_model` {#plan-export-codegen-model}

The plan as the code generator needs it: every event type with its name rule,
fields and events, and the documented variables.

```json
{
  "format": "codegen_model",
  "revision": "3f9c2a1e-…",
  "branch": "main",
  "branch_id": "0b7d…",
  "plan_hash": "sha256:51c0…",
  "event_types": [
    {
      "name": "se",
      "display_name": "Structured events",
      "name_rule": "{category}:{action}:{label}",
      "fields": [
        { "name": "category", "required": true, "type": "string", "values": null, "variable": null },
        { "name": "plan", "required": false, "type": "enum", "values": ["annual", "monthly"], "variable": null }
      ],
      "events": [
        {
          "identity": "promo_sheet:tap:${promo_slot}",
          "name": "promo_sheet:tap:${promo_slot}",
          "status": "live",
          "field_values": { "category": "promo_sheet", "action": "tap", "label": "${promo_slot}" },
          "deprecated": false,
          "overrides": {}
        }
      ]
    }
  ],
  "variables": [
    { "name": "promo_slot", "allowed_values": ["cart", "home"], "tokens": ["promo_slot"] }
  ]
}
```

| Key | Meaning |
|-----|---------|
| `event_types[].name_rule` | The type's resolved event name format, or `null` for a type identified by a flat name. |
| `fields[].type` | The plan field type: `string`, `number`, `boolean`, `json`, `enum` or `url`. |
| `fields[].values` | The closed set of values the plan allows for the field across the type's events, or `null` when it is free. It is closed only for a string-like field that **every** exported event fills with a literal, a variable with allowed values, or a template whose holes all have them; an event that leaves the field unset makes it free. A free enum field falls back to its options. |
| `fields[].variable` | The variable the field is bound to, or `null`. |
| `events[].field_values` | Plan field to value. A `${token}` value is a variable placeholder; its values are in `variables`. |
| `events[].deprecated` | `true` for a deprecated event. Archived events are not listed. |
| `events[].overrides` | The event's own allowed values for a variable, keyed by every `${token}` spelling of it, values in plan order. For that event only they replace the variable's `allowed_values`; an empty list means the event accepts any value. `{}` when the event overrides nothing. |
| `variables[].allowed_values` | The variable's documented values. |
| `variables[].tokens` | Every `${token}` spelling that names the variable, so a stored `field_values` template can be mapped back to it. |

## Safe Agent Defaults

- Use a project-scoped `read` key for retrieval agents.
- Use a project-scoped `write` key only for agents that are explicitly allowed to edit the tracking plan.
- Pass `branch=<branch_id>` for all write calls unless the operator intentionally wants to edit main.
- Search first, then fetch the canonical entity by id before making decisions.
- Before a delete, deprecate or rename, check `GET /projects/{slug}/dependencies` (or `POST /impact` for several changes) and report what it names; the write itself will not stop you.
- Before you add tracking code for an event, or after you change it, validate the calls with `POST /projects/{slug}/plan/validate`. It needs only a `read` key.
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
