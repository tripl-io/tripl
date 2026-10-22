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
The `codequality` and `junit` reports, `--report` and exit code `4` on this page
are newer than tripl v0.3.2. The CLI and the server are released together under
one version number: pin the CLI to the version your instance runs, and upgrade
the two together.
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
Then run the check locally until it finds your calls:

```bash
export TRIPL_BASE_URL=https://tripl.example.com
export TRIPL_API_KEY=tk_r_...
uvx tripl@X.Y.Z check
```

Replace `X.Y.Z` with your instance's version. The last line counts what was
checked:

```text
214 call sites in 58 files: 190 ok, 3 warnings, 2 errors, 19 only known at runtime (listed with --strict).
```

- **"nothing was checked"** (exit 2) means the configuration matched no call: a
  `sources:` glob that points nowhere, or a call spec that names a function your
  code does not call. A check that finds nothing must not look like a pass, so
  it fails unless you pass `--allow-empty`.
- **"only known at runtime"** counts calls whose name or values the scanner
  could not read. `--strict` lists them. Point the configuration at the place
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
  image: python:3.12-slim
  stage: test                  # any stage your pipeline has
  needs: []                    # start at once; it does not need the build
  variables:
    TRIPL_BASE_URL: https://tripl.example.com
  rules:
    - if: $TRIPL_API_KEY == null       # a merge request from a fork has no key: skip
      when: never
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH   # what merge requests are compared with
  before_script: []            # none of the build's own setup
  cache: []
  script:
    - pip install --quiet tripl==X.Y.Z
    - tripl check --report codequality=gl-code-quality-report.json --report junit=tripl-check.xml
  artifacts:
    when: always
    expire_in: 30 days
    reports:
      codequality: gl-code-quality-report.json
      junit: tripl-check.xml
  allow_failure: true          # report only, for now: step 5 makes it block
```

- **The image only needs Python.** The check reads source files as text and
  compiles nothing, so it needs no SDK, no Gradle and no Xcode. `before_script:
  []` and `cache: []` keep a pipeline-wide setup out of this job.
- **The run on the default branch matters.** GitLab compares a merge request's
  report with the default branch's, and shows only the findings the merge
  request adds or fixes. The plan changes in tripl, not in git, so also run the
  pipeline on a schedule (nightly, say) to keep that comparison current.
- **Merge request pipelines.** The rules assume your project runs them. With
  branch pipelines only, use `- if: $CI_COMMIT_BRANCH` instead of the
  `merge_request_event` rule.
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
[uv](https://docs.astral.sh/uv/) does. Write the report your CI reads:

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
- **Everywhere:** the job log lists every finding with its file and line.

A finding keeps its identity when the code above it moves. An edit elsewhere in
the file does not report it again as new.

## 5. Make it block merges

When the default branch has no errors left, remove `allow_failure: true`. In
GitLab, also turn on **Pipelines must succeed** in the merge request settings.
Then let an outage of tripl pass while a finding still blocks:

```yaml
  allow_failure:
    exit_codes: [4]    # tripl was unavailable, so nothing was checked
```

| Exit code | Meaning |
|-----------|---------|
| 0 | No errors. Warnings pass unless you add `--strict`. |
| 1 | At least one error (or warning, with `--strict`). Also a refused key or an unknown project: a retry would not fix those. |
| 2 | A configuration problem, including a configuration that matched no call. |
| 4 | The instance was unavailable: unreachable, timed out, or it answered `429` or a `5xx`. Nothing was checked. |

GitHub Actions has no list of exit codes to allow; end the command with
`|| [ $? -eq 4 ]` for the same effect.

Every error fails the run wherever it is, not only in the lines a merge request
changed, so get the default branch clean first.

**More detail:** the [`tripl check` guide](../integrate/tripl-check.md) covers
the configuration, presets, enums, every finding and the
[report formats](../integrate/tripl-check.md#output). For checking captured
events from UI tests instead of source code, see
[payload mode](../integrate/tripl-check.md#modes).
