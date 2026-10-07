# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for how sync works elsewhere.

The JSONL exports are gitignored on purpose: this repository is public, and the export carries the maintainer's email plus write-ups naming the private production host. Beads is local-only here — the owner's decision, after a push published the whole issue database — so do not run `bd dolt push` or `bd dolt remote add`; `bd dolt commit` is the only sync-shaped command to use. Do not re-add the exports.

## Session Completion

Before ending a session, leave nothing only on this machine that someone else needs: file beads issues for follow-up work, close what is finished, and make sure committed work is on a pushed branch.

Changes reach `main` through a pull request unless the owner asks for a direct push: a merge happens on green CI and with the owner's go-ahead. Merging `main` does not deploy the product (production is deployed by hand); it does publish the docs site to GitHub Pages and the `ghcr.io/tripl-io/tripl:preview` image, which the public demo follows through the Enterprise preview. Before every push, `git diff origin/main...HEAD -- .beads/` must be empty — `bd` sometimes commits a config change to local `main` on its own. Pushing a branch is a git operation only; it never includes `bd dolt push`.
<!-- END BEADS INTEGRATION -->


## Build & Test

Build, test, lint, and migration commands live in **[CONTRIBUTING.md](CONTRIBUTING.md)** —
the single source of truth. The one rule worth repeating: the backend uses `uv`
(`uv.lock`) and the frontend uses `bun` (`bun.lock`); never use `pip`,
`poetry`, `npm`, `pnpm`, or `yarn`, or the lockfiles drift from CI.

## Architecture Overview

See **[AGENTS.md](AGENTS.md)** for the fast navigation map (repo layout, domain
model, API map, async pipeline) and **[website/docs/develop/architecture.md](website/docs/develop/architecture.md)**
for the longer architecture write-up. Product scope and concepts live in
**[website/docs/use/concepts.md](website/docs/use/concepts.md)**.

## Conventions & Patterns

See the **Practical Coding Guidance** section of [AGENTS.md](AGENTS.md) and the
contributor workflow in [CONTRIBUTING.md](CONTRIBUTING.md). In short: keep FastAPI
routers thin (logic in services), use Celery for heavy/scheduled work, preserve the
async request path / sync worker split, and keep backend Pydantic schemas and
frontend TS types in sync.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
