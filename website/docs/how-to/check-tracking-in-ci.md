---
title: Check tracking code in CI
sidebar_label: Check tracking in CI
sidebar_position: 12
description: Run tripl check on every merge request in GitLab, GitHub or any other CI, and see unknown events and values the plan does not allow where reviewers look.
---

# Check tracking code in CI

**You will:** check every merge request against the tracking plan. An event the
plan does not know, a field it does not define or a value it does not allow
shows up in the merge request, next to the line that sends it, before it reaches
the warehouse.

**You need:** a repository whose tracking calls are in Swift, Objective-C,
Kotlin, Java, TypeScript or JavaScript, CI runners that can reach your tripl
instance, and an account in the project's organization.

:::note A CLI released after v0.3.2
This page needs a tripl CLI newer than v0.3.2: the `codequality` and `junit`
reports, `--report`, `--list` and exit code `4` came after it. The CLI and the server are
released together under one version number: pin the CLI to the version your
instance runs, and upgrade the two together.
:::

## 1. Create a read-only key for CI

A key acts as the person who created it, so create it as a **bot user**: an
ordinary member of the organization made for CI (say `ci@your-company.com`), a
**viewer** on the projects it checks. Then nobody's departure breaks the
pipeline.

Sign in as that user, open the settings, then **API keys**, and create a key
with the scope **Read-only** and the one **Project** it checks. Copy it: it starts
with `tk_r_` and is shown only once. A read-only key cannot change anything; the
check only reads the plan.

## 2. Find your tracking calls on your machine first

Write `.tripl/check.yml` at the root of the repository: which functions send
which event type, and which argument carries the name and the fields. The
[`tripl check` guide](../integrate/tripl-check.md#config-file) has every key.

Start with `--list`. It needs no key and sends nothing. It shows every call the
config matches, what the scan read from it and why a value was not read, the
wrappers that forward their own parameter, and the calls that look like
tracking but match no call spec. Run before there is a config, it lists the
calls in the repository that look like tracking: a place to start.

```bash
uvx tripl@X.Y.Z check --list
```

The [guide](../integrate/tripl-check.md#list) says what to do about each reason
a value is not read. When `--list` shows the calls you expect, run the real
check:

```bash
export TRIPL_BASE_URL=https://tripl.example.com
export TRIPL_API_KEY=tk_r_...
uvx tripl@X.Y.Z check
```

Replace `X.Y.Z` with your instance's version. The summary line counts what was
checked:

```text
214 call sites in 58 files: 190 ok, 3 warnings, 2 errors, 19 only known at runtime (listed with --strict).
```

A **Not read** line can follow it, naming configured files the scan skipped
because they were too large or could not be read.

- **"nothing was checked"** (exit 2) means the configuration matched no call: a
  `sources:` glob that points nowhere, or a call spec that names a function your
  code does not call. A check that finds nothing must not look like a pass, so
  it fails unless you pass `--allow-empty`.
- **"only known at runtime"** counts calls whose name or values the scanner
  could not read. `--strict` lists them, and `--list` says why a value was not
  read. Point the configuration at the place
  the name is written (often a constructor or a factory rather than the
  function that finally sends the event) and the count drops.
- **Errors** found now are already in your code. Fix them, add the missing
  events to the plan, or mark the ones you are retiring as deprecated (a
  warning, not an error) before you make the check block merges in step 5.

## 3. Add the job

### GitLab CI/CD {#gitlab}

Add the key under **Settings → CI/CD → Variables**: key `TRIPL_API_KEY`,
visibility **Masked and hidden**, and **Protect variable** off. A protected
variable never reaches a merge request pipeline from an ordinary branch, so a
protected key would skip the check on every merge request. Then add the job to
`.gitlab-ci.yml`:

```yaml
tripl-check:
  image: ghcr.io/tripl-io/tripl-cli:X.Y.Z
  stage: test                  # any stage your pipeline has
  needs: []                    # start at once; it does not need the build
  variables:
    TRIPL_BASE_URL: https://tripl.example.com
  rules:
    - if: $TRIPL_API_KEY == null       # a merge request from a fork has no key: skip
      when: never
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH   # what merge requests are compared with
      allow_failure: true      # the default branch and its schedules only report
  before_script: []            # none of the build's own setup
  cache: []
  script:
    # The merge request's base: the target branch's commit in a merged results
    # pipeline or a merge train, else the merge base. Empty on the default branch.
    - BASE="${CI_MERGE_REQUEST_TARGET_BRANCH_SHA:-$CI_MERGE_REQUEST_DIFF_BASE_SHA}"
    - '[ -z "$BASE" ] || git fetch --no-tags --depth=1 origin "$BASE"'
    - tripl check --new-from-rev "$BASE" --report codequality=gl-code-quality-report.json --report junit=tripl-check.xml
  artifacts:
    when: always
    expire_in: 30 days
    reports:
      codequality: gl-code-quality-report.json
      junit: tripl-check.xml
  allow_failure: true          # report only, for now: step 5 makes it block
```

- **The image is the CLI.** `ghcr.io/tripl-io/tripl-cli` is Python with the
  CLI installed, git, a shell and no entrypoint, tagged with each CLI release
  (`X.Y.Z`, `X.Y` and `latest`): the job installs nothing. The check reads
  source files as text and compiles nothing, so it needs no SDK, no Gradle and
  no Xcode. Runners that cannot pull from `ghcr.io` can use `python:3.12-slim`
  and start the script with `apt-get update && apt-get install -y git` and
  `pip install --quiet tripl==X.Y.Z`.
  `before_script: []` and `cache: []` keep a pipeline-wide setup out of this
  job.
- **Only what the merge request adds fails it.** `--new-from-rev "$BASE"`
  compares the checkout with the merge request's base commit, which the
  `git fetch` line brings into the shallow clone, and fails the job only on
  findings the merge request brings in: a new or edited call, or a constant or
  enum it changed. Legacy findings and a plan edit on main never fail it. Never
  use `CI_COMMIT_BEFORE_SHA` as the base: it covers only the last push, so a
  red merge request could turn green without a fix. See
  [Gate on the change](../integrate/tripl-check.md#new-findings).
- **The run on the default branch matters.** GitLab compares a merge request's
  report with the default branch's, and shows only the findings the merge
  request adds or fixes. The plan changes in tripl, not in git, so also run the
  pipeline on a schedule (nightly, say) to keep that comparison current. There
  `BASE` is empty, so there is no gate (the job log says
  `--new-from-rev is empty: every finding counts.`) and the run reports every
  finding; the rule's `allow_failure: true` keeps it from failing those
  pipelines. It stays that way until a baseline file can mark the findings the
  default branch already has.
- **Merge request pipelines.** The rules assume your project runs them. With
  branch pipelines only, there is no merge request base: gate each branch on
  its merge base with the default branch, and keep the default-branch rule
  first (rules match in order) so the default branch still only reports:

  ```yaml
    variables:
      GIT_DEPTH: 0             # the merge base needs the branch's history
    rules:
      - if: $TRIPL_API_KEY == null
        when: never
      - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
        allow_failure: true
      - if: $CI_COMMIT_BRANCH
    script:
      - BASE=""
      - |
        if [ "$CI_COMMIT_BRANCH" != "$CI_DEFAULT_BRANCH" ]; then
          git fetch --no-tags origin "$CI_DEFAULT_BRANCH"
          BASE="$(git merge-base FETCH_HEAD HEAD)"
        fi
      - tripl check --new-from-rev "$BASE" --report codequality=gl-code-quality-report.json --report junit=tripl-check.xml
  ```
- **The address.** `TRIPL_BASE_URL` must be the instance's final `https://`
  address. The check sends a `POST`, and a redirect is refused rather than
  followed. The runners must be able to reach it: an instance behind a firewall
  needs runners inside it.

### GitHub Actions {#github-actions}

Add the key as a repository secret, `TRIPL_READ_ONLY_KEY`, then the workflow:

```yaml
# .github/workflows/tracking-plan.yml
name: Tracking plan

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  security-events: write       # to upload SARIF
  actions: read                # upload-sarif needs it on a private repository

jobs:
  tripl-check:
    # A pull request from a fork gets no secrets: skip it instead of failing.
    if: github.event_name == 'push' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v6
      - name: Check tracking calls against the plan
        env:
          TRIPL_BASE_URL: https://tripl.example.com
          TRIPL_API_KEY: ${{ secrets.TRIPL_READ_ONLY_KEY }}
        run: uvx tripl@X.Y.Z check --report sarif=tripl-check.sarif
      - name: Upload findings to code scanning
        if: always() && hashFiles('tripl-check.sarif') != ''
        uses: github/codeql-action/upload-sarif@v4
        with:
          sarif_file: tripl-check.sarif
          category: tripl-check
```

The upload runs even when the check failed, which is when its findings matter.
A run that could not finish writes no report, and the `hashFiles` condition then
skips the upload. Code scanning on a private repository needs GitHub Code
Security. Without it, read the findings in the job log, or add `--report
junit=tripl-check.xml` for a test-report action.

### Any other CI {#other-ci}

The check is one command with an exit code, so it runs wherever Python 3.12 or
[uv](https://docs.astral.sh/uv/) does, and in the
`ghcr.io/tripl-io/tripl-cli:X.Y.Z` image wherever a job can run in a
container. Write the report your CI reads:

| CI | Report | Where it goes |
|----|--------|---------------|
| Jenkins | `junit`, or `sarif` | `junit 'tripl-check.xml'`; or, with the Warnings Next Generation plugin, `recordIssues tools: [sarif(pattern: 'tripl-check.sarif')]` |
| Azure Pipelines | `junit` | `PublishTestResults@2` with `testResultsFormat: JUnit` and `testResultsFiles: tripl-check.xml` |
| Bitbucket Pipelines | `junit` | write it under `test-results/` (`--report junit=test-results/tripl-check.xml`), where Pipelines looks for test reports |
| CircleCI | `junit` | a `store_test_results` step with `path: tripl-check.xml` |
| TeamCity | `junit` | `##teamcity[importData type='junit' path='tripl-check.xml']` |

One run writes every report you name, from one validation: `--report
junit=tripl-check.xml --report sarif=tripl-check.sarif` costs no more than one
of them.

## 4. Read the results

- **GitLab:** the merge request shows a **Code Quality** report with the findings
  it adds and the ones it fixes, each linked to its line, and a **Test summary**
  that lists each failing call as a failed test. Both work on every tier; GitLab
  Ultimate also marks the lines in the diff.
- **GitHub:** findings are code scanning alerts on the lines of the pull request.
- **Everywhere:** the job log lists every finding with its file and line;
  under `--new-from-rev`, only the ones new in the change and what fails the
  run, with a `Gate:` line counting the rest. The Code Quality and JUnit reports
  list every finding either way, so GitLab's comparison with the default branch
  sees the whole picture; a legacy failure can therefore show as a failed test
  in a job that passed.

A finding keeps its identity when the code above it moves. An edit elsewhere in
the file does not report it again as new.

## 5. Make it block merges

With the gate a merge request fails only on what it adds, so the job can
block merges before the default branch is clean: remove the job's
`allow_failure: true` and keep the one on the default-branch rule. In GitLab,
also turn on **Pipelines must succeed** in the merge request settings. Then let
an outage of tripl pass while a finding still blocks:

```yaml
  allow_failure:
    exit_codes: [4]    # tripl was unavailable, so nothing was checked
```

| Exit code | Meaning |
|-----------|---------|
| 0 | No errors, or only [suppressed](#accept-one-call) ones. Warnings pass unless you add `--strict`. |
| 1 | At least one error (or warning, with `--strict`). Also a refused key or an unknown project: a retry would not fix those. |
| 2 | A configuration problem, including a configuration that matched no call. |
| 4 | The instance was unavailable: unreachable, timed out, or it answered `429` or a `5xx`. Nothing was checked. |

GitHub Actions has no list of exit codes to allow; end the command with
`|| [ $? -eq 4 ]` for the same effect.

Exit 4 covers every failure to reach the instance, so a mistyped host name in
`TRIPL_BASE_URL`, a DNS failure or a certificate the runner does not trust
passes as well. GitLab shows such a job as **passed with warnings**: open it
after you change the address or the runners.

Without `--new-from-rev`, every error fails the run wherever it is, not only
where a merge request changed something, so get the default branch clean
first. Exit 2 also covers a gate that cannot read the change: the base commit
is not in the clone (the message says how to fetch it: by SHA, as `origin/<branch>` for a branch, or with more history for a name like `HEAD^1`), git is
missing, or git refuses a checkout another user owns (the message names the
`safe.directory` command; the CLI never bypasses it).

The gate in other CI systems: the clone must hold the base commit, then pass
it to `--new-from-rev`. `HEAD^1` is the target branch's side of the merge
commit a pull request build checks out; a clone of depth 1 does not have it,
and no `git fetch` of `HEAD^1` can bring it (git fetches commits and branches,
not relative names), so check out with a depth of 2.

| CI | Base for `--new-from-rev` | The clone it needs |
|----|---------------------------|--------------------|
| GitHub Actions | `HEAD^1` on the pull request's merge ref | `fetch-depth: 2` in `actions/checkout` |
| Azure Pipelines | `HEAD^1` (a pull request build checks out the merge commit) | `fetchDepth: 2` on the `checkout: self` step (new pipelines fetch depth 1) |
| Bitbucket Pipelines | `"$BITBUCKET_PR_DESTINATION_COMMIT"` | `clone: depth: full`: the variable is an abbreviated SHA, which only a clone that already holds the commit can resolve (no fetch can ask a remote for an abbreviation) |
| Jenkins | `"$(git merge-base origin/$CHANGE_TARGET HEAD)"` | the target branch fetched with its history (no shallow clone) |
| Locally | `"$(git merge-base origin/main HEAD)"` |
| Another VCS | none: write the change as a diff in git's format (`hg diff --git`, `svn diff --git`, or `git diff --no-index BASE_DIR .` in the checkout) and pass `--new-from-patch FILE` (or `-` for stdin) |

```yaml
# GitHub Actions: the check step, gated on the pull request
- uses: actions/checkout@v7
  with:
    fetch-depth: 2
- run: uvx tripl@X.Y.Z check --new-from-rev HEAD^1 --report sarif=tripl-check.sarif
```

Running the image locally on a host checkout, pass your uid so git sees the
checkout as yours, and the instance's address and key, which a container does
not inherit:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e TRIPL_BASE_URL -e TRIPL_API_KEY \
  -v "$PWD:/src" ghcr.io/tripl-io/tripl-cli:X.Y.Z tripl check --new-from-rev HEAD
```

## Accept one call on purpose {#accept-one-call}

Some calls are wrong on purpose: a debug-only event, or a field the plan will
gain next week. Instead of leaving the job red, mark the call with a comment
that says which finding to accept and why:

```kotlin
// tripl-check-ignore[unknown_event] -- debug-only event, stripped from release builds (APP-12)
analytics.track("debug_tap", mapOf("screen" to "home"))
```

The finding stops failing the run. It stays in the Code Quality, JUnit, SARIF
and JSON reports, marked as suppressed with its reason, and the job log counts
it in the summary (`1 finding suppressed`). The reason after `--` is required: a marker without one suppresses
nothing, and the job log says so. A marker that no longer matches any finding
is listed as unused, so you can remove it. Reviewers see the marker in the
diff like any other code, which keeps each exception a decision someone made.
The guide's [Suppress a finding](../integrate/tripl-check.md#suppress) has the
full grammar and the comment forms for each language.

**More detail:** the [`tripl check` guide](../integrate/tripl-check.md) covers
the configuration, presets, enums, every finding and the
[report formats](../integrate/tripl-check.md#output). For checking captured
events from UI tests instead of source code, see
[payload mode](../integrate/tripl-check.md#modes).
