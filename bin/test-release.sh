#!/usr/bin/env bash
# Tests bin/release.sh on throwaway repositories: a bare origin at 9.9.8 and a
# fresh clone of it for each case. `uv` and `curl` are stand-ins on PATH (the
# lock records the project's version; PyPI "has" tripl once a flag file
# exists), so nothing leaves the temporary directory. CI runs it.
#
#   bin/test-release.sh
#
# Requires git 2.28 or newer and python3.
set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/release.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# No user or system configuration (signing, hooks, default branch) applies.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com

stubs="$work/stubs"
mkdir -p "$stubs"
cat > "$stubs/uv" <<'SH'
#!/bin/sh
[ "$1" = lock ] || { echo "uv stand-in: unexpected arguments: $*" >&2; exit 1; }
grep -E '^version = ' pyproject.toml > uv.lock
SH
cat > "$stubs/curl" <<'SH'
#!/bin/sh
[ -e "$PYPI_FLAG" ]
SH
chmod +x "$stubs/uv" "$stubs/curl"
export PATH="$stubs:$PATH"
export PYPI_FLAG="$work/pypi-has-tripl"

origin="$work/origin.git"
clone="$work/clone"
seed="$work/seed"
out=""
status=0
TAGS=(v9.9.9 cli-v9.9.9 mcp-v9.9.9)

fail() {
  echo "FAIL: $*" >&2
  echo "--- release.sh said:" >&2
  echo "$out" >&2
  exit 1
}

# A clone of origin as it is now, on main.
fresh() {
  rm -rf "$clone"
  git clone -q "$origin" "$clone"
}

# Runs release.sh in the clone; its output in $out, its exit status in $status.
release() {
  out="$(cd "$clone" && bin/release.sh "$@" 2>&1)" && status=0 || status=$?
}

refused() {
  [[ "$status" -ne 0 ]] || fail "$subject: release.sh did not refuse"
  grep -qF -- "$1" <<< "$out" || fail "$subject: the output does not say '$1'"
}

# What a refusal or an undone release must leave: none of the three tags
# anywhere, the clone at the commit it started on with a clean tree, origin's
# main where it was.
untouched() {
  local start="$1" origin_main="$2" t
  for t in "${TAGS[@]}"; do
    ! git -C "$clone" rev-parse -q --verify "refs/tags/$t" >/dev/null || fail "$subject: the clone has the tag $t"
    ! git -C "$origin" rev-parse -q --verify "refs/tags/$t" >/dev/null || fail "$subject: origin has the tag $t"
  done
  [[ "$(git -C "$clone" rev-parse HEAD)" == "$start" ]] || fail "$subject: the clone's HEAD moved"
  git -C "$clone" diff --quiet HEAD || fail "$subject: the clone's tree has changes"
  [[ "$(git -C "$origin" rev-parse main)" == "$origin_main" ]] || fail "$subject: origin's main moved"
}

# The two OpenAPI documents as bin/sync-api-types.sh writes them: the backend
# snapshot with sorted keys, the docs-site copy in the app's own order, both
# with a trailing newline.
backend_openapi() {
  printf '{\n  "info": {\n    "title": "tripl",\n    "version": "%s"\n  },\n  "paths": {}\n}\n' "$1"
}
docs_openapi() {
  printf '{\n  "openapi": "3.1.0",\n  "info": {\n    "version": "%s",\n    "title": "tripl"\n  }\n}\n' "$1"
}

# --- origin, at 9.9.8 ---
git init -q --bare -b main "$origin"
git init -q -b main "$seed"
mkdir -p "$seed/bin" "$seed/backend" "$seed/frontend" "$seed/cli" "$seed/mcp-server" "$seed/website/openapi"
cp "$SCRIPT" "$seed/bin/release.sh"
printf '[project]\nname = "tripl-backend"\nversion = "9.9.8"\n' > "$seed/backend/pyproject.toml"
printf '[project]\nname = "tripl"\nversion = "9.9.8"\n' > "$seed/cli/pyproject.toml"
printf '[project]\nname = "tripl-mcp"\nversion = "9.9.8"\ndependencies = [\n    "tripl>=9.9.8,<10",\n]\n' \
  > "$seed/mcp-server/pyproject.toml"
for dir in backend cli mcp-server; do
  grep -E '^version = ' "$seed/$dir/pyproject.toml" > "$seed/$dir/uv.lock"
done
printf '{\n  "name": "tripl-frontend",\n  "version": "9.9.8",\n  "devDependencies": {\n    "x": "1.0.0"\n  }\n}\n' \
  > "$seed/frontend/package.json"
backend_openapi 9.9.8 > "$seed/backend/openapi.json"
docs_openapi 9.9.8 > "$seed/website/openapi/tripl.openapi.json"
git -C "$seed" add -A
git -C "$seed" commit -q -m "9.9.8"
git -C "$seed" push -q "$origin" main

subject="dry run"
fresh
start="$(git -C "$clone" rev-parse HEAD)"
release -n 9.9.9
[[ "$status" -eq 0 ]] || fail "$subject: exited $status"
grep -qF "[dry-run]" <<< "$out" || fail "$subject: no dry-run plan"
untouched "$start" "$start"

subject="another branch"
fresh
git -C "$clone" checkout -q -b topic
release -y --no-wait 9.9.9
refused "releases are made from main"
untouched "$start" "$start"

subject="uncommitted changes"
fresh
echo "# local" >> "$clone/backend/pyproject.toml"
release -y --no-wait 9.9.9
refused "working tree is dirty"
git -C "$clone" checkout -q -- backend/pyproject.toml
untouched "$start" "$start"

subject="main ahead of origin"
fresh
git -C "$clone" commit -q --allow-empty -m "not pushed"
ahead="$(git -C "$clone" rev-parse HEAD)"
release -y --no-wait 9.9.9
refused "main is not origin/main (1 ahead, 0 behind)"
untouched "$ahead" "$start"

subject="main behind origin"
fresh
git -C "$seed" commit -q --allow-empty -m "merged meanwhile"
git -C "$seed" push -q "$origin" main
start_origin="$(git -C "$origin" rev-parse main)"
release -y --no-wait 9.9.9
refused "main is not origin/main (0 ahead, 1 behind)"
untouched "$start" "$start_origin"
start="$start_origin"

subject="a local tag origin does not have"
fresh
git -C "$clone" tag cli-v9.9.9
release -y --no-wait 9.9.9
refused "tag cli-v9.9.9 exists here but not on origin"
git -C "$clone" tag -d cli-v9.9.9 >/dev/null
untouched "$start" "$start"

subject="origin refuses the push"
fresh
mkdir -p "$origin/hooks"
printf '#!/bin/sh\necho "refused by the test" >&2\nexit 1\n' > "$origin/hooks/pre-receive"
chmod +x "$origin/hooks/pre-receive"
release -y --no-wait 9.9.9
rm "$origin/hooks/pre-receive"
refused "undoing the local commit and tags"
untouched "$start" "$start"

subject="a release, before tripl is on PyPI"
fresh
release -y --no-wait 9.9.9
[[ "$status" -eq 0 ]] || fail "$subject: exited $status"
tagged="$(git -C "$origin" rev-parse 'refs/tags/v9.9.9^{commit}')" || fail "$subject: origin has no tag v9.9.9"
[[ "$(git -C "$origin" rev-parse 'refs/tags/cli-v9.9.9^{commit}')" == "$tagged" ]] \
  || fail "$subject: origin's cli-v9.9.9 is not the tagged commit"
[[ "$(git -C "$origin" rev-parse main)" == "$tagged" ]] || fail "$subject: origin's main is not the tagged commit"
[[ "$(git -C "$clone" rev-parse HEAD)" == "$tagged" ]] || fail "$subject: the clone is not at the tagged commit"
for t in v9.9.9 cli-v9.9.9; do
  [[ "$(git -C "$origin" cat-file -t "refs/tags/$t")" == "tag" ]] || fail "$subject: $t is not an annotated tag"
done
# mcp-v waits for PyPI: made here, not pushed, and the command to push it printed.
! git -C "$origin" rev-parse -q --verify refs/tags/mcp-v9.9.9 >/dev/null || fail "$subject: origin has mcp-v9.9.9"
git -C "$clone" rev-parse -q --verify refs/tags/mcp-v9.9.9 >/dev/null || fail "$subject: the clone has no mcp-v9.9.9"
grep -qF "git push origin mcp-v9.9.9" <<< "$out" || fail "$subject: no command to push mcp-v9.9.9 later"
for file in backend/pyproject.toml cli/pyproject.toml mcp-server/pyproject.toml; do
  git -C "$origin" show "$tagged:$file" | grep -qx 'version = "9.9.9"' || fail "$subject: $file"
done
git -C "$origin" show "$tagged:mcp-server/pyproject.toml" | grep -qF '"tripl>=9.9.9,<10"' \
  || fail "$subject: tripl-mcp's requirement on tripl"
for dir in backend cli mcp-server; do
  git -C "$origin" show "$tagged:$dir/uv.lock" | grep -qx 'version = "9.9.9"' || fail "$subject: $dir/uv.lock"
done
git -C "$origin" show "$tagged:frontend/package.json" | grep -qF '"version": "9.9.9"' \
  || fail "$subject: frontend/package.json"
cmp -s <(git -C "$origin" show "$tagged:backend/openapi.json") <(backend_openapi 9.9.9) \
  || fail "$subject: backend/openapi.json is not the 9.9.8 document at 9.9.9, byte for byte"
cmp -s <(git -C "$origin" show "$tagged:website/openapi/tripl.openapi.json") <(docs_openapi 9.9.9) \
  || fail "$subject: website/openapi/tripl.openapi.json is not the 9.9.8 document at 9.9.9, byte for byte"
git -C "$clone" diff --quiet HEAD || fail "$subject: the clone's tree has changes"

subject="a tag origin already has"
fresh
release -y --no-wait 9.9.9
refused "origin already has tag v9.9.9"
[[ "$(git -C "$clone" rev-parse HEAD)" == "$tagged" ]] || fail "$subject: the clone's HEAD moved"

subject="a release, once tripl is on PyPI"
fresh
touch "$PYPI_FLAG"
release -y 9.10.0
[[ "$status" -eq 0 ]] || fail "$subject: exited $status"
mcp="$(git -C "$origin" rev-parse 'refs/tags/mcp-v9.10.0^{commit}')" || fail "$subject: origin has no mcp-v9.10.0"
[[ "$mcp" == "$(git -C "$origin" rev-parse 'refs/tags/v9.10.0^{commit}')" ]] \
  || fail "$subject: mcp-v9.10.0 is not the release commit"

echo "bin/release.sh: all cases passed"
