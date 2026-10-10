# MCP Server

tripl ships a first-party [Model Context Protocol](https://modelcontextprotocol.io)
server, **`tripl-mcp`**, that exposes a curated slice of the
[Agent API](./agent-api-guide.md) as typed MCP tools. It lives in `mcp-server/`
(Python module `tripl_mcp`) and is a **standalone client of a running tripl
instance** — it holds no database access and no privileged backdoor. Every tool
call becomes a normal `/api/v1` request authenticated with a tripl API key.

Because of that, the MCP server inherits the API-key security model wholesale:

- **Scope** — a `tk_r_` key makes every write tool fail; a `tk_w_` key is
  required for mutations and is still subject to the user role behind it.
- **Project fencing** — a project-scoped key confines all tools to that one
  `/projects/{slug}/...` namespace.
- **Roles** — editor-only routes still require an editor or owner user behind
  the key.
- **Owner and instance-administration operations are impossible by design** —
  those routes reject API keys entirely, so no MCP tool can reach them no
  matter how the server is configured.

## Configuration

The server is configured through environment variables:

| Property | Required | Meaning |
|----------|----------|---------|
| `TRIPL_BASE_URL` | yes | Base URL of the tripl instance, e.g. `https://tripl.example.com` |
| `TRIPL_API_KEY` | stdio mode | API key (`tk_r_...` or `tk_w_...`) used for all requests |
| `TRIPL_MCP_ALLOW_MAIN` | no | Set to `1` to let plan-mutating tools run without a `branch_id` (i.e. write to the main branch). Off by default — leave it off. |

Give a discovery-only agent a `tk_r_` key and prefer project-scoped keys, the
same [safe defaults](./agent-api-guide.md#safe-agent-defaults) as for raw REST.

:::tip Same two properties as the CLI
`TRIPL_BASE_URL` and `TRIPL_API_KEY` are read identically by the
[operator CLI](../run/cli.md), and the two tools share one HTTP client (the MCP
server imports it from the `tripl` distribution in `cli/`). A shell configured
for one is configured for the other, so `tripl doctor` is the quickest way to
prove the URL and key an MCP client is about to use actually work — including
whether the key is fenced to a single project.

`tripl-mcp` checks `TRIPL_BASE_URL` at startup exactly as the CLI does: a value
without `http://` or `https://` stops the server with the fix, and a pasted
trailing `/api/v1` is dropped. Use the final address, usually `https://`: a
write tool that gets a redirect fails rather than being re-sent as a `GET`.
:::

## Running over stdio

Stdio is the default transport: the MCP client launches `tripl-mcp` as a child
process and the key comes from the environment.

:::note Which install paths work
All of them, with no checkout of your own — but they get the HTTP client from
different places. `uvx tripl-mcp` installs the current release and resolves the
`tripl` distribution from the index, as an ordinary dependency of the published
wheel. The `uvx --from git+…` form clones the whole repository, so the sibling
`cli/` arrives with it and `tripl` is built from the same commit as the server
rather than from the index — that form pins the two halves together and does not
depend on what PyPI currently serves.

Between releases the repository can be ahead of what PyPI serves — take the git
form when you need something that has landed but not yet shipped. This page
names no version numbers on purpose: they were hand-maintained and went stale on
the first release that followed.

The **container** image is published either way —
`ghcr.io/tripl-io/tripl-mcp`, built alongside the app image on every release —
and is what `docker compose --profile mcp up` pulls.
:::

### Claude Code

```bash
claude mcp add tripl \
  -e TRIPL_BASE_URL=https://tripl.example.com \
  -e TRIPL_API_KEY=tk_r_... \
  -- uvx tripl-mcp
```

Replace `uvx tripl-mcp` with
`uvx --from 'git+https://github.com/tripl-io/tripl.git#subdirectory=mcp-server' tripl-mcp`
to run the repository copy rather than the release.

Or check a `.mcp.json` into the project (put the key itself in your shell
environment, not in the file):

```json
{
  "mcpServers": {
    "tripl": {
      "command": "uvx",
      "args": ["tripl-mcp"],
      "env": {
        "TRIPL_BASE_URL": "https://tripl.example.com",
        "TRIPL_API_KEY": "${TRIPL_API_KEY}"
      }
    }
  }
}
```

### Claude Desktop

Add the same block under `mcpServers` in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tripl": {
      "command": "uvx",
      "args": ["tripl-mcp"],
      "env": {
        "TRIPL_BASE_URL": "https://tripl.example.com",
        "TRIPL_API_KEY": "tk_r_..."
      }
    }
  }
}
```

Running from a source checkout instead of an installed package:

```bash
uv run --project /path/to/tripl/mcp-server tripl-mcp --transport stdio
```

## Running over streamable HTTP

For shared or containerized deployments, start the server once and let many
clients connect:

```bash
tripl-mcp --transport streamable-http --port 8765
```

In this mode the server does **not** use a stored key. Each client request must
carry its own `Authorization: Bearer tk_...` header, which the server passes
through to the tripl API for that request only — credentials are never stored
server-side. Different agents can therefore share one MCP server while keeping
distinct keys, scopes, and project fences.

`--host` is the bind address. The default, `127.0.0.1`, is reachable from the
same machine only; `--host 0.0.0.0` listens on every interface, which is what
the `mcp` service of `compose.yaml` uses inside its container (its port is
published on the host's `127.0.0.1` only; see
[the compose stack](../run/deployment.md#the-compose-stack)).

On a loopback bind the MCP SDK accepts only the `Host` names `127.0.0.1`,
`localhost` and `[::1]`, and answers anything else with
**`421 Invalid Host header`**. This is a guard against DNS rebinding. A reverse
proxy that forwards the public host name hits it: Caddy and Traefik do by
default, and nginx does with `proxy_set_header Host $host` (nginx's own default
sends the `proxy_pass` address, which passes). Name the public host instead of
turning the check off:

```bash
tripl-mcp --transport streamable-http --port 8765 --allowed-host mcp.example.com
```

`--allowed-host` is repeatable and takes an exact name or `HOST:*` for any
port; `--allowed-origin ORIGIN` (for example `https://agents.example.com`) does
the same for the `Origin` header, which only browser clients send. Either flag
turns the checks on whatever `--host` is, and the loopback names stay accepted,
so on `--host 0.0.0.0` every public name has to be listed.

The dev compose stack includes an optional, profile-gated `mcp` service that
builds `mcp-server/` and points it at the `api` container. It never starts by
default:

```bash
docker compose -f compose.dev.yaml --profile mcp up
```

## Toolset

The v1 toolset is deliberately curated rather than a 1:1 mirror of the OpenAPI
contract. Read tools carry the MCP `readOnlyHint` annotation so runtimes can
treat them as safe to auto-approve.

### Enumerated arguments

Arguments the API constrains to a fixed set are declared as enums in the tool
schema, not as plain strings. An MCP client refuses a value outside the set
locally, before any request — so a misspelled filter costs no round trip and
surfaces as a schema error the agent can correct, rather than as the route's
`422`.

| Argument | Tools | Accepted values |
|----------|-------|-----------------|
| `status` | `list_events` (repeatable), `create_event` | `draft`, `in_review`, `ready_for_dev`, `implemented`, `live`, `deprecated`, `archived` |
| `order_by` | `list_events` | `catalog` — the authored order, and what omitting the argument gets — or `volume`, busiest-first by ingested volume over the last 24 hours, or `health`, least healthy first by the [health score](../use/health-score.md) (main plan only) |
| `types` | `search_plan` (repeatable) | `event`, `event_type`, `field`, `meta_field`, `variable`, `relation`, `tag`, `metric`, `fact_table`, `scan_config`, `alert_rule`, `doc` |
| `scope` | `list_docs`, `read_doc`, `search_docs`, `write_doc` | `project`, `organization` |
| `audience` | `list_docs` | `human`, `agent`, `both` |

These lists are the API's own, checked against `backend/openapi.json` by the
MCP server's contract test rather than kept in step by hand, so they cannot
drift from what the routes accept — the same guarantee the
[operator CLI](../run/cli.md)'s `--status`, `--order-by` and `--type` choices
carry. `audience` is the one filter applied by the tool rather than by the
route, and it is held to the API's `DocAudience` all the same.

`update_event` is the one exception. Its `patch` is a free-form object rather
than a set of named arguments, so a `status` inside it is validated by the API
and not by the tool schema.

### Read tools

| Tool | Arguments | Backed by |
|------|-----------|-----------|
| `search_plan` | `slug, q, types?, limit?, branch_id?` | `GET /projects/{slug}/search` — `types` is [enumerated](#enumerated-arguments) |
| `list_events` | `slug, search?, status?, tag?, field_value?, meta_value?, event_type_id?, silent_since_days?, reviewed?, has_open_questions?, property?, offset?, limit?, order_by?, branch_id?` | `GET /projects/{slug}/events` — `status` and `order_by` are [enumerated](#enumerated-arguments); rows keep `title`, `open_question_count` and `last_seen_at` |
| `get_event` | `slug, event_id, branch_id?` | `GET /projects/{slug}/events/{event_id}` |
| `get_event_properties` | `slug, event_id, branch_id?` | `GET /projects/{slug}/events/{event_id}/properties` |
| `list_event_types` | `slug, branch_id?` | `GET /projects/{slug}/event-types` — **trimmed**: identity, `event_name_format` (the naming rule that may replace a sent name) and `field_count` |
| `get_event_type_fields` | `slug, event_type_id, branch_id?` | Event type + its field definitions, merged |
| `list_variables` | `slug, branch_id?, offset?, limit?` | `GET /projects/{slug}/properties` |
| `get_variable_values` | `slug, variable_id, branch_id?` | Property values + event overrides |
| `list_branches` | `slug` | `GET /projects/{slug}/branches` |
| `get_branch_diff` | `slug, branch_id` | `GET /projects/{slug}/branches/{branch_id}/diff` |
| `list_scans` | `slug` | `GET /projects/{slug}/scans` — **trimmed**: identity, schedule and a derived `dispatchable` flag, without `base_query` and the tuning knobs |
| `get_scan` | `slug, scan_id` | `GET /projects/{slug}/scans/{scan_id}` — one config in full, including everything `list_scans` trims |
| `get_scan_status` | `slug, scan_id, job_id?` | Scan job listing, or one job when `job_id` is given |
| `monitors_summary` | `slug` | Monitors summary + top anomaly signals, combined |
| `reconciliation_status` | `slug` | Reconciliation coverage + dead/shadow events as count + sample, keeping the window (`days`) and the untriaged shadow count (`new_count`) |
| `list_projects` | — | `GET /api/v1/projects` |
| `list_docs` | `slug, scope?, audience?` | `GET /projects/{slug}/docs` — both roots unless `scope` narrows it; **trimmed** rows without content. A note marked `both` matches either `audience` |
| `read_doc` | `slug, scope, path, lang?` | `GET /projects/{slug}/docs/file` — raw Markdown with frontmatter, parsed fields, revision, and every `[[link]]` with its status on the main plan. Without `lang`, the project's agent language when that translation is up to date, else the original; `lang` names a stored translation or `original` ([Translations](../use/docs-catalog.md#translations)) |
| `search_docs` | `slug, q, scope?, limit?` | `GET /projects/{slug}/docs/search` — ranked, not paged; `limit` at most 50 |

:::warning `list_scans` changed shape in 0.2.0
It used to return the whole `ScanConfigResponse` for every config — 30-odd
fields including the raw `base_query` SQL and every tuning knob — which is a
large payload to spend an agent's context on when the question is usually "which
scans exist, and are they scheduled". It now returns a trimmed projection plus a
derived `dispatchable` flag.

Nothing became unreachable: `get_scan` returns one config in full. If your agent
read a field off the listing, point it at `get_scan` for the config it actually
cares about.
:::

:::note
`list_projects` is instance-level: with a **project-scoped** key it returns
`403` by design. That is expected behavior, not a broken setup — use the
project the key is fenced to.
:::

:::note
`list_variables` passes the API response straight through, so it returns the
paged envelope `{"items": [...], "total": <int>}` — the first 200 properties of
the project. Compare `total` against `len(items)` before concluding a property
does not exist; on a large catalog, fall back to `search_plan` with
`types=variable` to find a specific one.

A missing property is not always a paging artefact either: a catalog scan run
can retire the scan-created properties nothing refers to any more, so an id from
an earlier listing can stop resolving. A scan started by hand always does this;
a scheduled collection does too, judging a property minted from a path inside a
JSON column on every run and one minted from a scalar column only when the
config declares a lookback window; a replay never. Properties that were edited, documented,
bound, or excluded from scans are never retired, and neither is one renamed to
anything the scan would not have chosen for that path itself — see
[Properties & templates](../use/variables-and-templates.md#unreferenced-scan-created-properties-are-retired-automatically).

The REST endpoint's `usage=all|used|unused` filter — `unused` being exactly the
set a retirement pass would take — is **not** plumbed through this tool, which
passes only `slug`, `branch_id`, `offset` and `limit`. An agent that needs
"which properties does nothing reference" has to call
`GET /api/v1/projects/{slug}/properties?usage=unused` over HTTP.
:::

### Write tools

Every write tool states in its description that it needs a `tk_w_` key; with a
`tk_r_` key these calls fail with `403`.

| Tool | Arguments | Backed by |
|------|-----------|-----------|
| `create_event` | `slug, branch_id, event_type_id, name, title?, description?, status?, tags?, field_values?, meta_values?` | `POST /projects/{slug}/events` — `status` is [enumerated](#enumerated-arguments) |
| `update_event` | `slug, event_id, branch_id, patch{...}` | `PATCH /projects/{slug}/events/{event_id}` — the patch accepts `title` alongside the other `EventUpdate` fields |
| `trigger_scan` | `slug, scan_id` | `POST /projects/{slug}/scans/{scan_id}/run` |
| `write_doc` | `slug, scope, path, content, base_revision?, message?` | `PUT /projects/{slug}/docs/file` — creates or replaces one note; `base_revision` makes a stale edit a `409` instead of an overwrite |

:::warning
In `update_event`, `field_values` and `meta_values` are **full-list
replacements**, exactly as in the REST contract: sending a partial list drops
the values you omitted. The tool description repeats this warning to the
agent. For narrow edits, patch only `description`, `title`, `name`, tags, or
state fields.
:::

### Team notes

The docs catalog holds Markdown notes next to the plan: warehouse gotchas, event
query recipes, conventions. A project shows its own notes (`scope: project`)
and its organization's notes (`scope: organization`). A note can say whom it is
for with `audience: agent` in its frontmatter, and `list_docs` with
`audience: agent` returns those notes plus the ones marked `both`. Hits also
appear in `search_plan` as entity type `doc`.

Notes are **not branch-aware**. A `write_doc` is live the moment it returns, on
the single version every reader sees, so there is no branch to pass and none to
review. That is why the toolset stops at single-note create and replace:

- Pass `base_revision` from the `read_doc` you edited. If someone changed the
  note since, the server answers `409` rather than overwriting their edit.
- A `[[kind:target]]` link that does not resolve comes back as a warning. The
  note is saved anyway, so fix the name and write it again. Plan links
  (`[[event:NAME]]`, `[[event-type:NAME]]`, `[[field:TYPE/NAME]]`,
  `[[variable:NAME]]`, `[[metric:NAME]]`, `[[branch:NAME]]`, `[[scan:NAME]]`,
  `[[data-source:NAME]]`) go by name. Links to notes, alert rules and people go
  by id: `[[doc:<id>]]`, `[[alert-rule:<id>]]` and `[[user:<id>]]` (a mention).
  See the [link syntax](agent-api-guide.md#docs-link-syntax).
- A `tk_r_` key, or a `tk_w_` key held by a viewer, gets a `403`.
- A key bound to one project gets a `403` on `scope: organization` writes,
  because every project of the organization reads those notes.
- The tools see what the key's user sees. A note that is not shared with that
  user (see [Sharing](../use/docs-catalog.md#sharing)) is missing from
  `list_docs`, `search_docs` and `search_plan`, and `read_doc` answers "not
  found" for it. The exception is a key of an organization owner or admin:
  `read_doc` still returns such a note, flagged `break_glass: true`, and the
  read is recorded in the audit log as `doc.break_glass_read`. It stays
  read-only and never shows in lists or searches. A note shared with the user
  to view only gets a `403` from `write_doc`. Sharing itself is changed in the
  app or through the API, not through MCP.

### Names an agent does not choose

Where a scan names the events of a type, `create_event` does not use the `name`
you send. The server builds it from the field values with that scan's format,
stamps it as the event's scan identity, and returns a warning saying yours was
ignored — read the response and adopt the returned `name` and `id`. Put the
human-readable label in `title` instead: it is stored beside the identity,
shown in lists and the branch diff, and never derived or overwritten.

Two failures follow from the same rule. A `422` means the format needs field
values the call did not carry, and names the columns. A `409` means an event
already answers to the derived name: only one event per identity ever receives
scan data, so the second would be inert. The identity is a unique key in the
database, so two creates racing for one name end the same way — the loser gets
the `409` naming the holder, never a second event. Open the event named in the
message rather than retrying — the call will not start succeeding.

### Branch safety

The Agent API guide's rule — never mutate main by accident — is encoded at the
tool layer: the plan-mutating tools (`create_event`, `update_event`) **require**
`branch_id`. Calls without a branch are rejected by the server itself unless
the operator explicitly sets `TRIPL_MCP_ALLOW_MAIN=1` in the server
environment. Discover branch ids with `list_branches` and review pending work
with `get_branch_diff`. Pick an open working branch (status `draft`,
`ready_for_review`, `changes_requested` or `approved`): a merged branch is
read-only, and a closed one is read-only until someone reopens it in the app,
so a write naming either comes back as a tool error carrying the backend's
`409` message.

## Deliberately not exposed in v1

The following surfaces exist in the REST API but are intentionally left out of
the MCP toolset:

- **Branch merge, transition, and revert** — landing a branch on main is a
  human review workflow. Agents propose changes on a branch; a person reviews
  the diff and merges in the app.
- **Reconciliation accept/dismiss** — accepting a shadow event into the plan or
  archiving dead events is a resolution decision. `reconciliation_status`
  surfaces the findings; a human acts on them.
- **SSE streaming endpoints** — MCP tool calls are request/response; streams do
  not map onto them usefully.
- **Multipart photo upload** — binary upload flows stay in the app and the raw
  REST API.
- **Moving, deleting, restoring and bulk-importing notes** — a deleted note
  takes its revision history with it, and an import can replace a whole folder.
  Agents create and replace single notes with `write_doc`; the rest stays in the
  app and in `tripl docs push`, which asks before it writes.

If your integration needs any of these, call the REST API directly per the
[Agent API guide](./agent-api-guide.md) — with the understanding that you are
then outside the curated safety rails.
