#!/usr/bin/env bash
# Release helper — bump version, commit, tag vX.Y.Z, push.
#
# Pushing the tag triggers .github/workflows/release.yml, which (after CI passes)
# builds the multi-arch image and pushes it to GHCR (ghcr.io/<owner>/tripl)
# tagged X.Y.Z / X.Y / latest, then cuts a GitHub Release. See website/docs/run/release.md.
#
#   bin/release.sh patch         # 0.1.0 -> 0.1.1
#   bin/release.sh minor         # 0.1.0 -> 0.2.0
#   bin/release.sh major         # 0.1.0 -> 1.0.0
#   bin/release.sh 1.4.0         # explicit version
#   bin/release.sh -n patch      # dry-run: print what would happen, change nothing
#   bin/release.sh -y minor      # skip the confirmation prompt
#
# Version source of truth: the git tag. backend/pyproject.toml and
# frontend/package.json are kept in sync by this script. Requires GNU sed.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bin/release.sh [-n|--dry-run] [-y|--yes] <patch|minor|major|X.Y.Z>

Bumps the version in backend/pyproject.toml and frontend/package.json, commits,
creates an annotated git tag vX.Y.Z, and pushes branch + tag to origin.
The tag push triggers the release workflow (build + push image to GHCR).
EOF
}

DRY_RUN=0
ASSUME_YES=0
BUMP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1; shift ;;
    -y|--yes)     ASSUME_YES=1; shift ;;
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

tag="v$new"
echo "Current version: $current"
echo "New version:     $new   (tag $tag)"

# --- preconditions ---
branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || echo "WARNING: not on 'main' (on '$branch')." >&2

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: working tree is dirty — commit or stash before releasing." >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "ERROR: tag $tag already exists." >&2
  exit 1
fi

# --- confirm (unless dry-run or -y) ---
if [[ "$DRY_RUN" -eq 0 && "$ASSUME_YES" -eq 0 ]]; then
  read -r -p "Release $tag from '$branch' and push to origin? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

apply_versions() {
  # pyproject.toml: only the [project] version line starts with `version = `
  # (ruff's `target-version` and mypy's `python_version` do not match ^version).
  sed -i -E 's/^version = "[^"]+"/version = "'"$new"'"/' "$PYPROJECT"
  # package.json: the top-level "version" field is the first "version": entry.
  sed -i -E '0,/"version": "[^"]+"/s//"version": "'"$new"'"/' "$PKG_JSON"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] would set version $current -> $new in $PYPROJECT and $PKG_JSON"
  echo "[dry-run] would commit 'chore(release): $tag', tag $tag, and push to origin/$branch"
  exit 0
fi

if [[ "$new" != "$current" ]]; then
  apply_versions
  git add "$PYPROJECT" "$PKG_JSON"
  git commit -m "chore(release): $tag"
else
  echo "Version files already at $new — tagging current HEAD without a bump."
fi
git tag -a "$tag" -m "Release $new"
git push origin "$branch"
git push origin "$tag"

cat <<EOF

Released $tag.
  • Release workflow is building + pushing ghcr.io/tripl-io/tripl:$new (and :latest)
  • Watch it:  gh run watch   (or the GitHub Actions tab)
  • Deploy:    export TRIPL_VERSION=$new && docker compose pull && docker compose up -d
               (an inline VAR=x prefix would apply to the pull only, so the
                'up' would start \${TRIPL_VERSION:-latest})
EOF
