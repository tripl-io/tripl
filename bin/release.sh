#!/usr/bin/env bash
# Release helper — one version for everything: bump, commit, tag, push.
#
# The service image, the operator CLI (`tripl` on PyPI) and the MCP server
# (`tripl-mcp` on PyPI) share one version. This script writes it everywhere,
# commits, and pushes three tags, each one triggering its own workflow:
#
#   vX.Y.Z      release.yml      CI gate, then ghcr.io/<owner>/tripl and -mcp
#                                tagged X.Y.Z / X.Y / latest, and a GitHub Release
#   cli-vX.Y.Z  publish-cli.yml  `tripl` to PyPI
#   mcp-vX.Y.Z  publish-mcp.yml  `tripl-mcp` to PyPI
#
# mcp-v goes last, after `tripl` X.Y.Z is on PyPI: tripl-mcp depends on it and
# publish-mcp.yml's clean-venv smoke test installs it from the index. The script
# waits for it (up to 30 minutes), or prints the command with --no-wait.
# See website/docs/run/release.md.
#
#   bin/release.sh patch         # 0.1.0 -> 0.1.1
#   bin/release.sh minor         # 0.1.0 -> 0.2.0
#   bin/release.sh major         # 0.1.0 -> 1.0.0
#   bin/release.sh 1.4.0         # explicit version
#   bin/release.sh -n patch      # dry-run: print what would happen, change nothing
#   bin/release.sh -y minor      # skip the confirmation prompt
#   bin/release.sh --no-wait minor  # do not wait for PyPI; print the mcp-v command
#
# Version source of truth: the git tags. The version files below are kept in
# sync by this script. Requires GNU sed, curl and uv.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bin/release.sh [-n|--dry-run] [-y|--yes] [--no-wait] <patch|minor|major|X.Y.Z>

Writes one version to the service, the CLI and the MCP server, refreshes their
uv.lock files, commits, and pushes the tags vX.Y.Z and cli-vX.Y.Z, then
mcp-vX.Y.Z once tripl X.Y.Z is on PyPI.
EOF
}

DRY_RUN=0
ASSUME_YES=0
WAIT_PYPI=1
BUMP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1; shift ;;
    -y|--yes)     ASSUME_YES=1; shift ;;
    --no-wait)    WAIT_PYPI=0; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *)            [[ -z "$BUMP" ]] || { echo "Unexpected extra argument: $1" >&2; usage; exit 2; }
                  BUMP="$1"; shift ;;
  esac
done
[[ -n "$BUMP" ]] || { usage; exit 2; }

# Always operate from the repo root, regardless of where we were invoked.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYPROJECT="backend/pyproject.toml"
PKG_JSON="frontend/package.json"
CLI_PYPROJECT="cli/pyproject.toml"
MCP_PYPROJECT="mcp-server/pyproject.toml"
# Each project's own uv.lock records its version; CI and the publish workflows
# run `uv ... --locked`, which fails when the lock no longer matches.
LOCK_DIRS=(backend cli mcp-server)

# --- current version (the [project].version line in backend/pyproject.toml) ---
current="$(grep -E '^version = "' "$PYPROJECT" | head -1 | sed -E 's/^version = "([^"]+)".*/\1/')"
[[ -n "$current" ]] || { echo "ERROR: could not read version from $PYPROJECT" >&2; exit 1; }
[[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "ERROR: current version '$current' is not X.Y.Z" >&2; exit 1; }

# --- compute the new version ---
if [[ "$BUMP" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  new="$BUMP"
else
  IFS=. read -r major minor patch <<<"$current"
  case "$BUMP" in
    major) new="$((major + 1)).0.0" ;;
    minor) new="${major}.$((minor + 1)).0" ;;
    patch) new="${major}.${minor}.$((patch + 1))" ;;
    *)     echo "ERROR: invalid bump '$BUMP'" >&2; usage; exit 2 ;;
  esac
fi
[[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "ERROR: bad target version '$new'" >&2; exit 1; }

# tripl-mcp accepts any `tripl` of the same compatible series: the same minor
# while the major is 0 (0.x minors may break), the same major after that.
IFS=. read -r new_major new_minor _ <<<"$new"
if [[ "$new_major" -eq 0 ]]; then
  cli_ceiling="0.$((new_minor + 1))"
else
  cli_ceiling="$((new_major + 1))"
fi
cli_range="tripl>=${new},<${cli_ceiling}"

tag="v$new"
cli_tag="cli-v$new"
mcp_tag="mcp-v$new"
echo "Current version: $current"
echo "New version:     $new   (tags $tag, $cli_tag, $mcp_tag)"
echo "tripl-mcp needs: $cli_range"

# --- preconditions ---
branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || echo "WARNING: not on 'main' (on '$branch')." >&2

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: working tree is dirty — commit or stash before releasing." >&2
  exit 1
fi
for t in "$tag" "$cli_tag" "$mcp_tag"; do
  if git rev-parse -q --verify "refs/tags/$t" >/dev/null; then
    echo "ERROR: tag $t already exists." >&2
    exit 1
  fi
done
command -v uv >/dev/null || { echo "ERROR: uv is required to refresh the lock files." >&2; exit 1; }

# --- confirm (unless dry-run or -y) ---
if [[ "$DRY_RUN" -eq 0 && "$ASSUME_YES" -eq 0 ]]; then
  read -r -p "Release $new ($tag, $cli_tag, $mcp_tag) from '$branch' and push to origin? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

apply_versions() {
  # pyproject.toml: only the [project] version line starts with `version = `
  # (ruff's `target-version` and mypy's `python_version` do not match ^version).
  sed -i -E 's/^version = "[^"]+"/version = "'"$new"'"/' "$PYPROJECT" "$CLI_PYPROJECT" "$MCP_PYPROJECT"
  # package.json: the top-level "version" field is the first "version": entry.
  sed -i -E '0,/"version": "[^"]+"/s//"version": "'"$new"'"/' "$PKG_JSON"
  # tripl-mcp's dependency on the CLI: the one `"tripl>=...` requirement line.
  sed -i -E 's/"tripl>=[^"]+"/"'"$cli_range"'"/' "$MCP_PYPROJECT"
  grep -qF "\"$cli_range\"" "$MCP_PYPROJECT" \
    || { echo "ERROR: could not set $cli_range in $MCP_PYPROJECT" >&2; exit 1; }
  # mcp-server resolves `tripl` from ../cli ([tool.uv.sources]), so this needs
  # no index and works before the CLI is published.
  for dir in "${LOCK_DIRS[@]}"; do
    (cd "$dir" && uv lock --quiet)
  done
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] would set version $current -> $new in $PYPROJECT, $PKG_JSON, $CLI_PYPROJECT, $MCP_PYPROJECT"
  echo "[dry-run] would set $cli_range in $MCP_PYPROJECT and refresh uv.lock in: ${LOCK_DIRS[*]}"
  echo "[dry-run] would commit 'chore(release): $tag', push origin/$branch, $tag and $cli_tag"
  echo "[dry-run] would push $mcp_tag once tripl $new is on PyPI"
  exit 0
fi

# Always applied, not only when the service's version moves: the CLI and the
# MCP server may lag behind it (a release cut before they shared its version).
apply_versions
if ! git diff --quiet; then
  git add "$PYPROJECT" "$PKG_JSON" "$CLI_PYPROJECT" "$MCP_PYPROJECT"
  for dir in "${LOCK_DIRS[@]}"; do git add "$dir/uv.lock"; done
  git commit -m "chore(release): $tag"
else
  echo "Version files already at $new — tagging current HEAD without a bump."
fi
git tag -a "$tag" -m "Release $new"
git tag -a "$cli_tag" -m "tripl $new"
git tag -a "$mcp_tag" -m "tripl-mcp $new"
git push origin "$branch"
git push origin "$tag" "$cli_tag"

pypi_has_cli() {
  curl -fsS "https://pypi.org/pypi/tripl/$new/json" >/dev/null 2>&1
}

if [[ "$WAIT_PYPI" -eq 1 ]]; then
  echo "Waiting for tripl $new on PyPI before pushing $mcp_tag (up to 30 minutes)..."
  for _ in $(seq 1 60); do
    pypi_has_cli && break
    sleep 30
  done
fi
if pypi_has_cli; then
  git push origin "$mcp_tag"
  mcp_line="  • $mcp_tag pushed: publish-mcp.yml is publishing tripl-mcp $new"
else
  mcp_line="  • tripl $new is not on PyPI yet. When it is:  git push origin $mcp_tag"
fi

cat <<EOF

Released $new.
  • $tag: release.yml is building ghcr.io/tripl-io/tripl:$new and tripl-mcp:$new (and :latest)
  • $cli_tag: publish-cli.yml is publishing tripl $new to PyPI
$mcp_line
  • Watch them:  gh run list --limit 5   (or the GitHub Actions tab)
  • Deploy:    export TRIPL_VERSION=$new && docker compose pull && docker compose up -d
               (an inline VAR=x prefix would apply to the pull only, so the
                'up' would start \${TRIPL_VERSION:-latest})
EOF
