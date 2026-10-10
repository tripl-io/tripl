---
title: Release Process
sidebar_position: 3
---

# Release Process

This page is for **maintainers** cutting a release. tripl ships as **one image**
that serves the JSON API and the built SPA in a single process, plus the MCP
server image and two Python packages, the operator CLI (`tripl`) and the MCP
server (`tripl-mcp`). **All of them share one version.** Releases are driven
entirely by **git tags**: `bin/release.sh` bumps the version everywhere and
pushes three tags, and each tag starts its own workflow.

```
bin/release.sh ─▶ vX.Y.Z      ─▶ release.yml      CI gate ─▶ buildx (amd64 + arm64)
                                                          ├─▶ ghcr.io/.../tripl and tripl-mcp :X.Y.Z, :X.Y, :latest
                                                          └─▶ GitHub Release
               ─▶ cli-vX.Y.Z  ─▶ publish-cli.yml  `tripl` to PyPI
               ─▶ mcp-vX.Y.Z  ─▶ publish-mcp.yml  `tripl-mcp` to PyPI (once `tripl` X.Y.Z is there)
```

The **git tags are the single source of truth** for the version.
`backend/pyproject.toml`, `frontend/package.json`, `cli/pyproject.toml`,
`mcp-server/pyproject.toml` and their `uv.lock` files are kept in sync by the
release script, not the other way around. The Enterprise image takes the same
version from its own repository.

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
bin/release.sh 0.4.0     # set an explicit version
bin/release.sh -n patch  # dry-run: print the plan, change nothing
bin/release.sh -y minor  # skip the confirmation prompt
bin/release.sh --no-wait minor  # do not wait for PyPI; print the mcp-v command
```

What [`bin/release.sh`](https://github.com/tripl-io/tripl/blob/main/bin/release.sh)
does:

1. Reads the current version from `backend/pyproject.toml` (the `[project]`
   `version` line).
2. Computes the new version and writes it to `backend/pyproject.toml`,
   `frontend/package.json`, `cli/pyproject.toml` and
   `mcp-server/pyproject.toml`, and sets `tripl-mcp`'s requirement on `tripl`
   to the new version's series (`>=X.Y.Z,<X.(Y+1)` while the major is 0).
3. Refreshes `uv.lock` in `backend/`, `cli/` and `mcp-server/`: each records
   its project's own version, and the CI and publish workflows run `uv
   --locked`. `mcp-server` resolves `tripl` from `../cli`, so this needs no
   published CLI.
4. Writes the version into the committed OpenAPI documents,
   `backend/openapi.json` and `website/openapi/tripl.openapi.json`
   (`info.version`).
5. Commits `chore(release): vX.Y.Z` with all of the above (skipped if every
   file is already at the target version — it then just tags the current
   `HEAD`). An explicit version equal to the service's current one still brings
   the CLI and the MCP server up to it.
6. Creates **annotated** tags `vX.Y.Z`, `cli-vX.Y.Z` and `mcp-vX.Y.Z`, and
   pushes `main`, `vX.Y.Z` and `cli-vX.Y.Z` to `origin` in **one atomic push**:
   origin takes all three or none. If anything fails before that push goes
   through, the local release commit and tags are undone, so running it again
   starts clean.
7. Waits (up to 30 minutes) for `tripl` X.Y.Z on PyPI, then pushes
   `mcp-vX.Y.Z`. If it is not there yet, or with `--no-wait`, it prints the
   `git push origin mcp-vX.Y.Z` to run later.

It runs from the repo root regardless of where you invoke it, and requires GNU
`sed`, `curl`, `python3` and `uv`. [`bin/test-release.sh`](https://github.com/tripl-io/tripl/blob/main/bin/test-release.sh)
tests the script, in CI's `scripts` job.

:::warning Preconditions
The script **refuses to run** unless you are on `main`, the working tree is
clean (no uncommitted or staged changes), and `main` is exactly `origin/main`
(it fetches first, and refuses a `main` that is ahead or behind). It also
refuses if any of the three tags already exists, on `origin` or only locally.
`-n`/`--dry-run` runs the same checks. Unless you pass `-n` or `-y`/`--yes`, it
prompts for confirmation before pushing.
:::

Use `-n` first if you are unsure — it prints exactly which version it would set
and what it would commit, tag, and push, without changing anything.

## What the tag triggers

Pushing a `v*` tag starts the
[Release workflow](https://github.com/tripl-io/tripl/blob/main/.github/workflows/release.yml),
which has four jobs:

1. **Release tag (`tag`)** — resolves the tag to its commit. It refuses a tag
   that is not `vX.Y.Z`, and a commit whose `backend/pyproject.toml` does not
   carry that version (a tag `bin/release.sh` did not make).
2. **CI gate (`ci`)** — runs the whole
   [`ci.yml`](https://github.com/tripl-io/tripl/blob/main/.github/workflows/ci.yml)
   via `workflow_call` on that commit (input `ref`): backend, CLI, MCP, the
   `bin/` scripts, warehouse conformance (PostgreSQL, ClickHouse, the BigQuery
   emulator, Greenplum, Trino), search relevance, digest concurrency, the
   Alembic round trip, frontend, end-to-end tests and the image builds. Both
   image jobs need it, so **no image is ever published from red code**.
3. **Build & push image (`image`)** — needs `tag` and `ci`:
   - Sets up QEMU + Buildx and logs in to GHCR.
   - Builds the tagged commit's root `Dockerfile` `runtime` stage for
     **`linux/amd64` and `linux/arm64`** and pushes to `ghcr.io/<owner>/tripl`.
   - Creates a **GitHub Release** from the tag with auto-generated notes.
4. **Build & push the MCP image (`mcp-image`)** — needs `tag` and `ci`, and
   builds the same commit for both platforms as `ghcr.io/<owner>/tripl-mcp`.

The `sha-<short>` image tag and the `org.opencontainers.image.revision` label
name the tagged commit, on a tag push and on a manual re-run alike.

The same `v*` tag also starts the five cloud value-conformance workflows
(`athena-`, `bigquery-`, `databricks-`, `redshift-` and
`snowflake-value-conformance.yml`). Each is off until its
`*_VALUE_CONFORMANCE_ENABLED` repository variable is `true`, then needs its
repository secrets; none of them gates the release. Their results are what
moves a warehouse from believed to proven in the
[capability matrix](../develop/warehouse-parity.md#read-this-first-proven-versus-believed).

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
The SPA is built natively on the runner's own platform (`$BUILDPLATFORM`) and
copied into both images; only `uv sync` and the runtime stages run emulated for
arm64. That still makes release builds slower than a native build, which is
acceptable for tagged releases. For faster builds later, move to a native arm64
runner.
:::

:::note First publish is private
The first push to a brand-new GHCR package creates it as **private**. Make it
public under *Packages → tripl → Package settings* if you want anonymous pulls.
:::

## Preview images

Every commit on `main` that passes CI is also published, by `preview.yml`, as
`ghcr.io/tripl-io/tripl:preview` (moves with `main`) and
`:preview-<commit>` (the first 12 characters of the commit). They are
linux/amd64 only and are not releases: no `latest`, no GitHub Release, nothing
on PyPI. Do not run them in production. An older commit whose CI finishes after
a newer one's never takes `preview` back.

The build has 15 minutes; it takes one or two. One that fails or runs out of
time is built once more, on a fresh builder and without the Actions layer
cache, where builds have stalled before (30 minutes), and the job gives up
after an hour. Runs wait for each other, so a stuck one would otherwise hold
every newer preview, and the demo, behind it.

The Enterprise edition builds its own preview on this image, and that is what
the public demo runs. Once the image is out, `preview.yml` asks the Enterprise
repository to build it. That takes `ENTERPRISE_DISPATCH_TOKEN` in the `preview`
environment (open to `main` only): a fine-grained token whose only permission is
*Actions: read and write* on the Enterprise repository. Without it, the step
says so and the Enterprise preview picks the image up on its own next run.

## The two Python packages

Two Python distributions ship to PyPI, each on its own tag, with the service's
version: `bin/release.sh` bumps and tags them together with the service.

| Distribution | Tag | Workflow | What it is |
|---|---|---|---|
| `tripl` | `cli-v*` | [`publish-cli.yml`](https://github.com/tripl-io/tripl/blob/main/.github/workflows/publish-cli.yml) | The [operator CLI](./cli.md) — `install`, `upgrade`, diagnostics, plan reads, scans and drifts, `check`, `codegen`, `export` and team notes |
| `tripl-mcp` | `mcp-v*` | [`publish-mcp.yml`](https://github.com/tripl-io/tripl/blob/main/.github/workflows/publish-mcp.yml) | The [MCP server](../integrate/mcp-server.md) for LLM agents |

The tags stay separate so that a failed publish can be re-run for one artifact
without touching the others.

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
0.2.0 was on the index. `bin/release.sh` therefore pushes `mcp-v*` only once
`tripl` of the same version is on PyPI.
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
| Owner | `tripl-io` |
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
2. Click **Run workflow** and pass the existing tag, e.g. `v0.3.1`.

On `workflow_dispatch` the `tag` job checks out the given tag and refuses
anything that is not `vX.Y.Z` or whose `backend/pyproject.toml` does not carry
that version. The CI gate and both image jobs then run on that tag's commit,
whichever branch **Use workflow from** names, and the tag goes into the
metadata action, so `X.Y.Z` / `X.Y` / `latest` still resolve correctly. The
`sha-<short>` image tag and the revision label name the tagged commit.

## Post-release smoke check

After the workflow goes green:

```bash
# 1. Watch / confirm the run finished
gh run watch

# 2. Confirm the Release was cut (X.Y.Z is the version you released)
gh release view vX.Y.Z

# 3. Confirm the multi-arch image is published (should list amd64 + arm64)
docker buildx imagetools inspect ghcr.io/tripl-io/tripl:X.Y.Z

# 4. Confirm the floating tags point at it
docker buildx imagetools inspect ghcr.io/tripl-io/tripl:latest
```

Then do a real pull-and-run on a throwaway host or locally, pinning the new tag:

```bash
TRIPL_VERSION=X.Y.Z docker compose pull
TRIPL_VERSION=X.Y.Z docker compose up -d
docker compose ps -a     # migrate and metrics-init one-shots Completed; app/workers healthy/Up
```

The stack from [`compose.yaml`](https://github.com/tripl-io/tripl/blob/main/compose.yaml)
is `postgres`, `rabbitmq`, `redis`, a one-shot `migrate` (`alembic upgrade head`
before anything starts), a one-shot `metrics-init` (clears stale Prometheus
multiprocess files from the shared volume, then exits like `migrate`), the `app`
(API + SPA on `:8000`), and `celery-worker` / `celery-beat` — all from the same
image. The `migrate`
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
the plan snapshot, and the order of two property overrides on such events.
Before it, namesakes came back in whatever order the database returned them, so their
place in the approval digest was never fixed; now they are ordered by id. An
approval recorded **before** that upgrade, on a branch holding namesakes, can
therefore read as `stale` afterwards even though nobody edited the plan, and the
branch needs approving again before it will merge. Nothing is lost and no data
is wrong; a digest cannot be recomputed backwards. Branches without namesakes,
and every approval given after the upgrade, are unaffected.
:::

:::note One-off: the CLI and MCP server read `/properties`
From the release after 0.3.1, `tripl` and `tripl-mcp` call
`/projects/{slug}/properties…` instead of the deprecated `/variables` alias.
`tripl plan properties`, and the MCP tools `list_variables` and
`get_variable_values`, need a server at v0.2.2 or later; against v0.2.1 and
older they answer `404`. From the same release, a CLI or MCP write that gets a
`3xx` fails with the redirect target named, instead of being re-sent as a
`GET`. Both belong in that release's notes.
:::
