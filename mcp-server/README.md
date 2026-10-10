# tripl-mcp

Standalone [MCP](https://modelcontextprotocol.io) server exposing a curated,
agent-safe toolset over a **running tripl instance**. It is a pure HTTP client
of the tripl REST API (`/api/v1`) — it imports no backend code and is not
mounted into the FastAPI app.

- Curated tools, not a mirror of the API: plan search; events (read, create,
  update) and their properties; event types and their fields; properties
  (variables) and their values; branches and branch diffs; scans (list, read,
  trigger, job status); monitors; reconciliation; projects; and the team's
  notes (list, read, search, write). Every tool and its arguments:
  <https://docs.tripl.io/integrate/mcp-server#toolset>.
- Read tools carry `readOnlyHint`. The write tools — `create_event`,
  `update_event`, `trigger_scan` and `write_doc` — need a `tk_w_` API key backed
  by an editor or owner.
- The two plan-mutating tools, `create_event` and `update_event`, **require
  `branch_id`** so an agent never edits the live main plan by accident.
  Operators can override with `TRIPL_MCP_ALLOW_MAIN=1`. The other two writes
  take effect at once: `write_doc` creates or replaces a note live (notes are
  not branch-aware), and `trigger_scan` starts a scan job.
- Branch merge/revert/transition, SSE streams, and photo upload are
  intentionally not exposed.
- The HTTP client is **not in this package**. It lives in the `tripl`
  distribution (`cli/` in the repository) and is imported from there, so the
  CLI and this server share one implementation rather than two that drift.

## stdio (Claude Code / Claude Desktop)

No form below needs a checkout of your own, but the first two get `tripl` — the
distribution this package imports its HTTP client from — from
different places, and the difference is worth knowing:

- `uvx tripl-mcp` installs the release from PyPI and resolves `tripl` from the
  index, as an ordinary dependency of the published wheel.
- The `git+…#subdirectory=mcp-server` form clones the **whole** repository, so
  the sibling `cli/` arrives with it and the `[tool.uv.sources]` table below
  points `tripl` at that directory. Server and client are therefore built from
  one commit, and the form does not care what the index currently serves.

> **This README describes `main`, which between releases can be ahead of what
> PyPI serves.** Deliberately no version numbers or tool counts here: they were
> hand-maintained and went stale the moment a release shipped. `uvx tripl-mcp`
> gives you the current release; the git form gives you what has landed but not
> yet shipped. `git log mcp-server/` is the honest diff between them.

From PyPI — the released version:

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

From git — no clone needed, and the way to run what is in this repository:

```json
{
  "mcpServers": {
    "tripl": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/tripl-io/tripl.git#subdirectory=mcp-server",
        "tripl-mcp"
      ],
      "env": {
        "TRIPL_BASE_URL": "https://tripl.example.com",
        "TRIPL_API_KEY": "tk_r_..."
      }
    }
  }
}
```

From a local checkout:

```json
{
  "command": "uv",
  "args": ["run", "--project", "/path/to/tripl/mcp-server", "tripl-mcp"]
}
```

Create API keys in the tripl app under **Settings → API keys**. Use a
project-scoped `tk_r_` key for read-only agents; reserve `tk_w_` keys for
agents explicitly allowed to edit the plan.

## streamable-http (shared server)

```bash
TRIPL_BASE_URL=https://tripl.example.com tripl-mcp --transport streamable-http --port 8765
```

In this mode the server holds **no credentials**. Every incoming MCP request
must carry `Authorization: Bearer tk_...`; the header is forwarded verbatim to
the tripl API and never stored. Requests without it get a clear tool error.

`--host` is the bind address. The default, `127.0.0.1`, is reachable from this
machine only; `--host 0.0.0.0` listens on every interface, which is what the
`mcp` service in tripl's `compose.yaml` runs with inside its container.

On a loopback bind the MCP SDK also checks each request's `Host` header against
the loopback names (`127.0.0.1`, `localhost`, `[::1]`) and answers anything else
with **`421 Invalid Host header`**. This guards against DNS rebinding, and it is
what a reverse proxy in front of the server runs into when it forwards the
public host name — Caddy and Traefik do by default, nginx does with
`proxy_set_header Host $host` (by default nginx sends the `proxy_pass` address,
which passes when that is `127.0.0.1:8765`). Name the public host instead of
turning the check off:

```bash
TRIPL_BASE_URL=https://tripl.example.com \
  tripl-mcp --transport streamable-http --port 8765 --allowed-host mcp.example.com
```

`--allowed-host HOST` (repeatable) accepts that `Host` value exactly, or any
port of it as `HOST:*`. `--allowed-origin ORIGIN` (repeatable, e.g.
`https://agents.example.com`) does the same for the `Origin` header, which only
browser-based clients send. Either flag turns the checks on whatever `--host`
is, and the loopback names stay accepted — so on `--host 0.0.0.0` every name
clients use to reach the server must be listed.

## Environment

| Variable | Meaning |
|----------|---------|
| `TRIPL_BASE_URL` | Base URL of the tripl instance (required). Checked at startup the same way `tripl` checks it; a trailing `/api/v1` is dropped. Use the final address, usually `https://`: a write tool that gets a redirect fails rather than being re-sent as a GET |
| `TRIPL_API_KEY` | API key for stdio mode (required for stdio) |
| `TRIPL_MCP_ALLOW_MAIN` | Set to `1` to allow plan writes without `branch_id` (edits main) |

## Development

```bash
cd mcp-server
uv sync
uv run --group dev pytest -q
uv run --group dev ruff check
uv run --group dev ruff format --check
uv run --group dev mypy src
```

`uv` resolves `tripl` from `../cli` via `[tool.uv.sources]`, so an edit there is
picked up with no install step — and must be, because `tripl_cli.client` is this
server's transport. **Run both suites after touching it**, which is what
`ci.yml`'s `cli` and `mcp` jobs do.

The container image builds from the **repository root**, not this directory,
for the same reason:

```bash
docker build -f mcp-server/Dockerfile .   # `docker build ./mcp-server` no longer works
```

## Docs

- Setup, every tool and its arguments:
  <https://docs.tripl.io/integrate/mcp-server>
- Agent workflow guidance:
  <https://docs.tripl.io/integrate/agent-api-guide#read-draft-write> and
  <https://docs.tripl.io/integrate/searching-from-the-api>
