#!/usr/bin/env bash
# Tests the two checks CI runs from bin/: check-junit.py, which proves a gate's
# pytest run executed, and check-openapi-docs.py, which keeps the docs site's
# API reference equal to backend/openapi.json. Each case writes its input to a
# temporary directory and asserts the exit status and the message. CI runs it.
#
#   bin/test-ci-checks.sh
#
# Requires python3.
set -euo pipefail

BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

out=""
status=0

fail() {
  echo "FAIL: $subject: $*" >&2
  echo "--- the check said:" >&2
  echo "$out" >&2
  exit 1
}

run() {
  out="$(python3 "$@" 2>&1)" && status=0 || status=$?
}

passes() {
  [[ "$status" -eq 0 ]] || fail "exited $status"
}

fails_saying() {
  [[ "$status" -ne 0 ]] || fail "did not fail"
  grep -qF -- "$1" <<< "$out" || fail "the output does not say '$1'"
}

# A JUnit report the way pytest writes one, from the testcase bodies given.
report() {
  local path="$work/$1"; shift
  {
    echo '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
    printf '%s\n' "$@"
    echo '</testsuite></testsuites>'
  } > "$path"
  echo "$path"
}

junit() {
  run "$BIN/check-junit.py" "$@"
}

ok='<testcase classname="t" name="test_ok"/>'
skip='<testcase classname="t" name="test_skip"><skipped type="pytest.skip" message="no db"/></testcase>'
xfail='<testcase classname="t" name="test_xfail"><skipped type="pytest.xfail" message="known"/></testcase>'
failure='<testcase classname="t" name="test_bad"><failure message="assert"/></testcase>'
error='<testcase classname="" name="t"><error message="collection failure"/></testcase>'

subject="check-junit: every test passed"
junit "$(report pass.xml "$ok" "$ok")" gate
passes
grep -qF "gate: 2 collected" <<< "$out" || fail "no summary line"

subject="check-junit: no report"
junit "$work/missing.xml" gate
fails_saying "no gate JUnit report"

subject="check-junit: a report that is not XML"
echo "not xml" > "$work/broken.xml"
junit "$work/broken.xml" gate
fails_saying "no gate JUnit report"

subject="check-junit: nothing collected"
junit "$(report empty.xml)" gate
fails_saying "0 gate tests ran"

subject="check-junit: a skip"
junit "$(report skip.xml "$ok" "$skip")" gate
fails_saying "1 gate test(s) skipped in CI: test_skip"

subject="check-junit: an xfail counts as a skip"
junit "$(report xfail.xml "$ok" "$xfail")" gate
fails_saying "1 gate test(s) skipped in CI: test_xfail"

subject="check-junit: an xfail with --allow-xfail"
junit "$(report xfail-allowed.xml "$ok" "$xfail")" gate --allow-xfail
passes
grep -qF "1 xfailed" <<< "$out" || fail "the xfail is not counted"

subject="check-junit: --allow-xfail still refuses a real skip"
junit "$(report skip-allowed.xml "$xfail" "$skip")" gate --allow-xfail
fails_saying "1 gate test(s) skipped in CI: test_skip"

subject="check-junit: a failure"
junit "$(report failure.xml "$ok" "$failure")" gate
fails_saying "1 gate test(s) failed or errored: test_bad"

subject="check-junit: a collection error"
junit "$(report error.xml "$error")" gate
fails_saying "1 gate test(s) failed or errored: t"

docs() {
  run "$BIN/check-openapi-docs.py" "$work/snapshot.json" "$work/docs.json"
}

printf '{\n  "info": {"title": "tripl", "version": "1.0.0"},\n  "paths": {"/a": {"get": {}}, "/b": {"get": {}}}\n}\n' > "$work/snapshot.json"

subject="check-openapi-docs: the same document in another key order"
printf '{\n  "paths": {"/b": {"get": {}}, "/a": {"get": {}}},\n  "info": {"version": "1.0.0", "title": "tripl"}\n}\n' > "$work/docs.json"
docs
passes

subject="check-openapi-docs: a path the docs copy lacks"
printf '{"info": {"title": "tripl", "version": "1.0.0"}, "paths": {"/a": {"get": {}}}}' > "$work/docs.json"
docs
fails_saying "path /b"

subject="check-openapi-docs: another version"
printf '{"info": {"title": "tripl", "version": "0.1.0"}, "paths": {"/a": {"get": {}}, "/b": {"get": {}}}}' > "$work/docs.json"
docs
fails_saying "top-level info"

echo "bin/check-junit.py and bin/check-openapi-docs.py: all cases passed"
