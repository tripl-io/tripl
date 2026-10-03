---
title: Release Process
sidebar_position: 3
---

# Release Process

This page is for **maintainers** cutting a release. tripl ships as **one image**
that serves the JSON API and the built SPA in a single process. Releases are
driven entirely by **git tags**: bump the version, push a `vX.Y.Z` tag, and
GitHub Actions runs CI, builds a multi-arch image, pushes it to GHCR, and cuts a
GitHub Release.

```
bin/release.sh        git tag vX.Y.Z        .github/workflows/release.yml
  bump version  ───▶   push to origin  ───▶   CI gate ─▶ buildx (amd64 + arm64)
                                                       ├─▶ push ghcr.io/.../tripl:X.Y.Z, :X.Y, :latest
                                                       └─▶ GitHub Release
```

The **git tag is the single source of truth** for the version.
`backend/pyproject.toml` and `frontend/package.json` are kept in sync by the
release script, not the other way around.

## Versioning & tags

tripl follows semver, and the tag is always `v` + the version:

| Bump | Meaning | Example |
|---|---|---|
| `patch` | Bug fixes, no API change | `0.1.0` → `0.1.1` |
| `minor` | New, backward-compatible features | `0.1.0` → `0.2.0` |
| `major` | Breaking changes | `0.1.0` → `1.0.0` |

You can also pass an explicit `X.Y.Z`. The script validates that both the
current and target versions are strict `X.Y.Z` (no pre-release suffixes), so
pre-releases are not part of this flow.

## Cut a release

From a clean `main` that is green on CI:

```bash
bin/release.sh patch     # 0.1.0 -> 0.1.1   (bug fixes)
bin/release.sh minor     # 0.1.0 -> 0.2.0   (features, back-compatible)
bin/release.sh major     # 0.1.0 -> 1.0.0   (breaking changes)
bin/release.sh 1.4.0     # set an explicit version
bin/release.sh -n patch  # dry-run: print the plan, change nothing
bin/release.sh -y minor  # skip the confirmation prompt
```

What [`bin/release.sh`](https://github.com/vladenisov/tripl/blob/main/bin/release.sh)
does:

1. Reads the current version from `backend/pyproject.toml` (the `[project]`
   `version` line).
2. Computes the new version and writes it to **both**
   `backend/pyproject.toml` and `frontend/package.json`.
3. Commits `chore(release): vX.Y.Z` (skipped if the files are already at the
   target version — it then just tags the current `HEAD`).
4. Creates an **annotated** tag `vX.Y.Z` and pushes the branch **and** the tag
   to `origin`.

It runs from the repo root regardless of where you invoke it, and requires GNU
`sed`.

:::warning Preconditions
The script **refuses to run** if the working tree is dirty (uncommitted or
staged changes) or if the tag already exists. It **warns** (but does not stop)
if you are not on `main`. Unless you pass `-n`/`--dry-run` or `-y`/`--yes`, it
prompts for confirmation before pushing.
:::

Use `-n` first if you are unsure — it prints exactly which version it would set
and what it would commit, tag, and push, without changing anything.

## What the tag triggers

Pushing a `v*` tag starts the
[Release workflow](https://github.com/vladenisov/tripl/blob/main/.github/workflows/release.yml),
which has two jobs:

1. **CI gate (`ci`)** — reuses
   [`ci.yml`](https://github.com/vladenisov/tripl/blob/main/.github/workflows/ci.yml)
   via `workflow_call`: the backend job (`ruff` + `mypy` + `pytest`) and the
   frontend job (`bun run lint` + `bun run build` + `bun run test`). The image job
   `needs: ci`, so **no image is ever published from red code**.
2. **Build & push image (`image`)** — after CI is green:
   - Sets up QEMU + Buildx and logs in to GHCR.
   - Builds the root `Dockerfile` `runtime` stage for **`linux/amd64` and
     `linux/arm64`** and pushes to `ghcr.io/<owner>/tripl`.
   - Creates a **GitHub Release** from the tag with auto-generated notes.

Authentication uses the built-in `GITHUB_TOKEN` (`packages: write` to push to
GHCR, `contents: write` to create the Release) — no extra secrets to manage.

### Image tags produced

Tags are computed by `docker/metadata-action`:

| Tag | Source | Notes |
|---|---|---|
| `X.Y.Z` | full semver | the exact release |
| `X.Y` | major.minor | floats to the latest patch |
| `latest` | `flavor latest=auto` | **stable releases only** (no pre-releases) |
| `sha-<short>` | commit | the exact build commit |

:::note arm64 is cross-built via QEMU
arm64 is emulated on the amd64 runner, so release builds are slower than a
native build (`bun run build` and `uv sync` especially). This is acceptable for
tagged releases. For faster builds later, move to a native arm64 runner.
:::

:::note First publish is private
The first push to a brand-new GHCR package creates it as **private**. Make it
public under *Packages → tripl → Package settings* if you want anonymous pulls.
:::

## The two Python packages

The service is not the only thing this repository releases. Two Python
distributions ship to PyPI on **their own tags**, and they version independently
of the service and of each other — a service release does not imply a CLI
release, and neither forces a version bump on the other.

| Distribution | Tag | Workflow | What it is |
|---|---|---|---|
| `tripl` | `cli-v*` | [`publish-cli.yml`](https://github.com/vladenisov/tripl/blob/main/.github/workflows/publish-cli.yml) | The [operator CLI](./cli.md) — `install`, `upgrade`, `doctor`, `status`, `watch`, `scans`, `drifts` |
| `tripl-mcp` | `mcp-v*` | [`publish-mcp.yml`](https://github.com/vladenisov/tripl/blob/main/.github/workflows/publish-mcp.yml) | The [MCP server](../integrate/mcp-server.md) for LLM agents |

`bin/release.sh` does **not** touch either: it versions the service. Release a
package by bumping its own `pyproject.toml` and pushing the matching tag.

```bash
# after bumping cli/pyproject.toml to the new version
git tag -a cli-v<version> -m "tripl <version>" && git push origin cli-v<version>
```

Both workflows gate before they publish: lint, type-check and the full test
suite (repeated from `ci.yml` on purpose, so a release is never the first time
they run), a check that the **tag matches the packaged version** — a mismatch
publishes a number that can never be reused on PyPI — and a smoke test that
installs the built wheel into a clean virtualenv and runs its console script.

:::note Why the smoke test refuses a local link
The smoke step installs from the index with **no** `--find-links`. That is the
one gate catching what a lockfile hides: `uv.lock` pins a working dependency
set, but a consumer gets only the declared constraints. `tripl-mcp` learned this
expensively — an unbounded `mcp` floor resolved to 2.0, where
`mcp.server.fastmcp` no longer exists, and the console script died on import.
Pointing the step at a sibling `dist/` would turn that gate into a gate that
hides the problem too.
:::

:::warning Publish `cli-v*` before `mcp-v*`
`tripl-mcp` imports its HTTP client from the `tripl` distribution and declares a
version floor on it. That floor is stated once, in `mcp-server/pyproject.toml`,
and deliberately not repeated here — a range copied into prose is a range that
goes stale.

Whenever the floor moves ahead of what the index serves, `publish-mcp.yml`'s
smoke test **fails on purpose**: it installs the built wheel from the index into
a clean virtualenv, so a wheel whose dependency cannot resolve is caught before
upload instead of by the first person to install it. The 0.2.0 release hit this
for real — `tripl-mcp` had begun importing `page_items` / `page_total`, which the
published `tripl` 0.1.0 does not export, so the mcp job failed until `tripl`
0.2.0 was on the index. Raise the floor and release `cli-v*` first.
:::

### Authentication: Trusted Publishing

Neither workflow stores an API token. PyPI verifies the workflow identity
(repository + workflow filename + environment) against a **publisher configured
on the project** and mints a short-lived credential for that one upload. A
leaked repository secret cannot publish, because there is no secret to leak.

The first release of a new distribution therefore needs a **pending publisher**
created on PyPI before the tag is pushed, under *Your projects → Publishing*:

| Field | Value |
|---|---|
| PyPI Project Name | `tripl` |
| Owner | `vladenisov` |
| Repository name | `tripl` |
| Workflow name | `publish-cli.yml` |
| Environment name | `pypi` |

Publishing runs only from a pushed tag; there is no manual run. If the publish
job fails transiently, open the tag's run in the Actions tab and use **Re-run
failed jobs** rather than re-tagging: a version number can never be reused on
PyPI.

## Re-running a build for an existing tag

If a push-triggered build fails transiently (e.g. a flaky network step), you do
not need to re-tag. The Release workflow also accepts **`workflow_dispatch`**:

1. Open the **Release** workflow in the GitHub Actions tab.
2. Click **Run workflow** and pass the existing tag, e.g. `v1.4.0`.

On `workflow_dispatch` the workflow checks out the requested tag and passes it
into the metadata action, so `X.Y.Z` / `X.Y` / `latest` still resolve
correctly. This re-runs the full CI gate before rebuilding.

## Post-release smoke check

After the workflow goes green:

```bash
# 1. Watch / confirm the run finished
gh run watch

# 2. Confirm the Release was cut
gh release view v1.4.0

# 3. Confirm the multi-arch image is published (should list amd64 + arm64)
docker buildx imagetools inspect ghcr.io/vladenisov/tripl:1.4.0

# 4. Confirm the floating tags point at it
docker buildx imagetools inspect ghcr.io/vladenisov/tripl:latest
```

Then do a real pull-and-run on a throwaway host or locally, pinning the new tag:

```bash
TRIPL_VERSION=1.4.0 docker compose pull
TRIPL_VERSION=1.4.0 docker compose up -d
docker compose ps        # migrate one-shot Completed; app/workers healthy/Up
```

The stack from [`compose.yaml`](https://github.com/vladenisov/tripl/blob/main/compose.yaml)
is `postgres`, `rabbitmq`, `redis`, a one-shot `migrate` (`alembic upgrade head`
before anything starts), the `app` (API + SPA on `:8000`), and
`celery-worker` / `celery-beat` — all from the same image. The `migrate`
one-shot must reach **Completed** before `app`/workers start, so a multi-worker
deploy never races the schema upgrade.

## Rollback & upgrade path

There is no separate rollback workflow — versions are immutable image tags, so
rolling back is just re-pinning `TRIPL_VERSION` to the previous release and
re-deploying. The full upgrade/downgrade procedure, required `.env` secrets, and
the production-hardening notes live on the deployment page:

➡️ See [Deployment](./deployment) for the upgrade and rollback steps.

:::danger Migrations are not auto-reversed
Downgrading the image does **not** roll back Alembic migrations. If a release
applied a schema change, rolling the image back to a version that predates that
migration can break against the upgraded schema. Treat schema changes as
forward-only and verify on a staging stack before relying on rollback.
:::

:::note One-off: approvals may need redoing after the plan-snapshot ordering fix
A branch approval pins a digest of the plan as it stood when the approval was
given, and the release that made
[multi-value meta fields](../use/feature-reference.md#event-types) hash in
sorted order changes those bytes for the events that carry one. An approval
recorded **before** that upgrade, on a branch whose events hold a multi-value
meta field whose values happened to be stored out of sorted order, therefore
reads as `stale` afterwards even though nobody edited the plan, and the branch
needs approving again before it will merge. Nothing is lost and no data is
wrong; a digest cannot be recomputed backwards, which is why the release takes
the re-approval rather than trying to. Branches with no multi-value meta field,
and every approval given after the upgrade, are unaffected.
:::

:::note One-off: approvals may need redoing on branches with same-named events or relations
The release that records which main row each branch copy came from (branch copy
origin ids) also fixes the order two **namesakes** — two events of one event
type sharing a name, or two relations linking the same pair of fields — take in
the plan snapshot, and the order of two variable overrides on such events.
Before it, namesakes came back in whatever order the database returned them, so their
place in the approval digest was never fixed; now they are ordered by id. An
approval recorded **before** that upgrade, on a branch holding namesakes, can
therefore read as `stale` afterwards even though nobody edited the plan, and the
branch needs approving again before it will merge. Nothing is lost and no data
is wrong; a digest cannot be recomputed backwards. Branches without namesakes,
and every approval given after the upgrade, are unaffected.
:::
