"""The CI templates in ``ci/``: run for real, held to the parser and to the release.

``ci/gitlab/tripl-check.yml`` is fetched with ``include: remote`` and
``ci/github/action.yml`` with ``uses:``, both from a ``cli-vX.Y.Z`` tag, so a
template must run the CLI of that tag and pass only flags that CLI has. Their
scripts are run here as a runner would run them, against real git
repositories, with ``tripl`` (or ``uvx``) replaced by a stub that records its
arguments: what each event passes as the gate's base, the baseline, and the
exit code a runner sees.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.test_contract import REPO_ROOT, _leaf_parsers
from tripl_cli.cli import build_parser

GITLAB = REPO_ROOT / "ci" / "gitlab" / "tripl-check.yml"
ACTION = REPO_ROOT / "ci" / "github" / "action.yml"
MARKER = " # tripl version: bin/release.sh sets it"
BASELINE = ".tripl/check-baseline.json"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _block(text: str, opener: str) -> str:
    """The literal block scalar that ``opener`` (a whole line, ending in ``|``) starts, dedented."""
    lines = text.splitlines()
    start = lines.index(opener) + 1
    indent = len(opener) - len(opener.lstrip()) + 2
    body: list[str] = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


GITLAB_SCRIPT = _block(GITLAB.read_text(encoding="utf-8"), "    - |")
ACTION_SCRIPT = _block(ACTION.read_text(encoding="utf-8"), "      run: |")


# --- held to the release and to the parser --------------------------------------------------------
@pytest.mark.parametrize("template", [GITLAB, ACTION], ids=["gitlab", "github"])
def test_a_template_runs_the_cli_release_it_ships_with(template: Path) -> None:
    # bin/release.sh rewrites this one line on every release; the tag then
    # holds a template that runs its own CLI.
    pyproject = tomllib.loads((REPO_ROOT / "cli" / "pyproject.toml").read_text(encoding="utf-8"))
    marked = re.findall(
        rf'"([^"]+)"{re.escape(MARKER)}$', template.read_text(encoding="utf-8"), re.M
    )
    assert marked == [pyproject["project"]["version"]]


@pytest.mark.parametrize("script", [GITLAB_SCRIPT, ACTION_SCRIPT], ids=["gitlab", "github"])
def test_every_flag_a_template_passes_is_a_check_flag(script: str) -> None:
    check = _leaf_parsers(build_parser())["tripl check"]
    known = {option for action in check._actions for option in action.option_strings}
    passed = set(
        re.findall(
            r'(?:set -- (?:"\$@"|check)|args\+=\(|\|\| args\+=\()[^\n]*?(--[a-z][a-z-]+)', script
        )
    )
    passed |= set(re.findall(r'"\$@" (--[a-z][a-z-]+)', script))
    assert passed >= {
        "--report",
        "--baseline",
        "--new-from-rev",
        "--strict",
        "--project",
        "--check-config",
    }
    assert passed <= known


def test_every_gitlab_input_is_declared_and_used() -> None:
    header, job = GITLAB.read_text(encoding="utf-8").split("\n---\n")
    declared = set(re.findall(r"^    ([a-z_]+):$", header, re.M))
    assert set(re.findall(r"inputs\.([a-z_]+)", job)) == declared
    defined = set(re.findall(r"^    (TRIPL_CHECK_[A-Z_]+):", job, re.M))
    assert set(re.findall(r"\$\{?(TRIPL_CHECK_[A-Z_]+)", GITLAB_SCRIPT)) == defined


def test_every_action_input_is_declared_and_used() -> None:
    text = ACTION.read_text(encoding="utf-8")
    inputs = text.split("\ninputs:\n")[1].split("\noutputs:\n")[0]
    declared = set(re.findall(r"^  ([a-z-]+):$", inputs, re.M))
    runs = text.split("\nruns:\n")[1]
    assert set(re.findall(r"inputs\.([a-z-]+)", runs)) == declared


# --- a repository as a runner clones it -----------------------------------------------------------
def _git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``main`` with a baseline, and ``feature`` one commit past it; any commit fetchable by SHA."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    repo = tmp_path / "origin"
    (repo / ".tripl").mkdir(parents=True)
    (repo / BASELINE).write_text("{}\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "a.swift").write_text("a\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "b.swift").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", "main")
    return repo


def _clone(origin: Path, at: str, dest: Path) -> Path:
    """A depth-1 checkout of ``at``, as a runner makes one."""
    dest.mkdir()
    _git(dest, "init", "-q")
    _git(dest, "remote", "add", "origin", origin.as_uri())
    _git(dest, "fetch", "-q", "--no-tags", "--depth=1", "origin", at)
    _git(dest, "checkout", "-q", "--detach", "FETCH_HEAD")
    return dest


def _stub(tmp_path: Path, name: str) -> Path:
    """``name`` on PATH: records its arguments, cwd and TRIPL_* env, and exits $STUB_EXIT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf "%s\\n" "$a"; done > "$STUB_OUT"\n'
        'pwd > "$STUB_OUT.cwd"\n'
        'printf "%s\\n%s\\n" "${TRIPL_BASE_URL:-}" "${TRIPL_API_KEY:-}" > "$STUB_OUT.env"\n'
        'for a in "$@"; do case "$a" in sarif=*) : > "${a#sarif=}" ;; esac; done\n'
        'exit "${STUB_EXIT:-0}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


class Ran:
    def __init__(self, done: subprocess.CompletedProcess[str], out: Path) -> None:
        self.code = done.returncode
        self.log = done.stdout + done.stderr
        self.called = out.exists()
        self.argv = out.read_text(encoding="utf-8").splitlines() if self.called else []
        self.cwd = Path(out.with_suffix(".cwd").read_text().strip()) if self.called else None
        self.env = out.with_suffix(".env").read_text().splitlines() if self.called else []

    def after(self, flag: str) -> str:
        return self.argv[self.argv.index(flag) + 1]


def _run(shell: str, script: str, cwd: Path, tmp_path: Path, stub: str, env: dict[str, str]) -> Ran:
    out = tmp_path / "argv"
    out.unlink(missing_ok=True)
    full = {
        "PATH": f"{_stub(tmp_path, stub)}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_OUT": str(out),
        **{k: v for k, v in os.environ.items() if k.startswith("GIT_")},
        **env,
    }
    done = subprocess.run([shell, "-c", script], cwd=cwd, env=full, capture_output=True, text=True)
    return Ran(done, out)


# --- GitLab ---------------------------------------------------------------------------------------
def _gitlab(clone: Path, tmp_path: Path, shell: str = "sh", **env: str) -> Ran:
    variables = {
        "CI_PROJECT_DIR": str(clone),
        "CI_DEFAULT_BRANCH": "main",
        "TRIPL_CHECK_BASE_URL": "",
        "TRIPL_CHECK_PROJECT": "",
        "TRIPL_CHECK_CONFIG": "",
        "TRIPL_CHECK_DIRECTORY": ".",
        "TRIPL_CHECK_BASELINE": BASELINE,
        "TRIPL_CHECK_ARGS": "",
        "TRIPL_CHECK_MODE": "pipelines=branches gate=true strict=false",
        **env,
    }
    return _run(shell, GITLAB_SCRIPT, clone, tmp_path, "tripl", variables)


MR_MODE = "pipelines=merge_requests gate=true strict=false"


def test_gitlab_branches_mode_never_opens_a_merge_request_pipeline() -> None:
    # Without workflow:rules, a job that matches merge_request_event opens a
    # merge request pipeline holding only it, and GitLab then checks only that
    # pipeline before a merge: the project's own tests would stop blocking.
    job = GITLAB.read_text(encoding="utf-8").split("\n---\n")[1]
    rules = re.findall(r"^    - if: (.+)$", job, re.M)
    guarded = [rule for rule in rules if "inputs.pipelines" in rule]
    assert len(guarded) == len(rules) - 1  # all but the missing-key rule
    branches = [rule for rule in guarded if '== "branches"' in rule]
    assert branches and not any("merge_request" in rule for rule in branches)
    assert re.search(r"default: branches$", GITLAB.read_text(encoding="utf-8"), re.M)


@needs_git
@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_gitlab_merge_request_is_gated_on_its_base_with_the_baseline(
    origin: Path, tmp_path: Path, shell: str
) -> None:
    base = _git(origin, "rev-parse", "main")
    clone = _clone(origin, "feature", tmp_path / "clone")
    ran = _gitlab(
        clone,
        tmp_path,
        shell,
        CI_PIPELINE_SOURCE="merge_request_event",
        CI_MERGE_REQUEST_IID="7",
        CI_MERGE_REQUEST_DIFF_BASE_SHA=base,
        TRIPL_CHECK_BASE_URL="https://tripl.example.com",
        TRIPL_CHECK_MODE=MR_MODE,
    )
    assert ran.code == 0, ran.log
    assert ran.argv == [
        "check",
        "--report",
        f"codequality={clone}/gl-code-quality-report.json",
        "--report",
        f"junit={clone}/tripl-check.xml",
        "--baseline",
        f"{clone}/{BASELINE}",
        "--new-from-rev",
        base,
    ]
    _git(clone, "cat-file", "-e", f"{base}^{{commit}}")  # fetched into the shallow clone
    assert ran.env[0] == "https://tripl.example.com"


@needs_git
def test_gitlab_merged_results_pipeline_uses_the_target_branch_commit(
    origin: Path, tmp_path: Path
) -> None:
    target = _git(origin, "rev-parse", "main")
    clone = _clone(origin, "feature", tmp_path / "clone")
    ran = _gitlab(
        clone,
        tmp_path,
        CI_MERGE_REQUEST_IID="7",
        CI_MERGE_REQUEST_TARGET_BRANCH_SHA=target,
        CI_MERGE_REQUEST_DIFF_BASE_SHA="0" * 40,
        TRIPL_CHECK_MODE=MR_MODE,
    )
    assert ran.code == 0, ran.log
    assert ran.after("--new-from-rev") == target


@needs_git
def test_gitlab_default_branch_has_no_gate_base_and_the_baseline_decides(
    origin: Path, tmp_path: Path
) -> None:
    clone = _clone(origin, "main", tmp_path / "clone")
    ran = _gitlab(clone, tmp_path, CI_COMMIT_BRANCH="main")
    assert ran.code == 0, ran.log
    assert ran.after("--new-from-rev") == ""  # the CLI says: every finding counts
    assert "--baseline" in ran.argv


@needs_git
def test_gitlab_branch_pipelines_gate_on_the_merge_base(origin: Path, tmp_path: Path) -> None:
    clone = _clone(origin, "feature", tmp_path / "clone")
    ran = _gitlab(
        clone,
        tmp_path,
        CI_COMMIT_BRANCH="feature",
    )
    assert ran.code == 0, ran.log
    assert ran.after("--new-from-rev") == _git(origin, "rev-parse", "main")


@needs_git
def test_gitlab_inputs_reach_the_command(origin: Path, tmp_path: Path) -> None:
    clone = _clone(origin, "main", tmp_path / "clone")
    ran = _gitlab(
        clone,
        tmp_path,
        CI_COMMIT_BRANCH="main",
        TRIPL_CHECK_PROJECT="ios app",
        TRIPL_CHECK_CONFIG="app/check.yml",
        TRIPL_CHECK_DIRECTORY="app",
        TRIPL_CHECK_BASELINE="none.json",
        TRIPL_CHECK_ARGS="--allow-empty  --branch *",
        TRIPL_CHECK_MODE="pipelines=merge_requests gate=false strict=true",
    )
    assert ran.code == 0, ran.log
    assert ran.argv[5:] == [
        "--project",
        "ios app",
        "--check-config",
        f"{clone}/app/check.yml",
        "--strict",
        "--allow-empty",
        "--branch",
        "*",  # not globbed
    ]
    assert ran.cwd == clone / "app"
    assert "No baseline at none.json" in ran.log


@needs_git
@pytest.mark.parametrize("code", [1, 2, 4])
def test_gitlab_job_exits_as_the_check_did(origin: Path, tmp_path: Path, code: int) -> None:
    # allow_failure: exit_codes reads it: 4 (tripl unavailable) passes with a warning.
    clone = _clone(origin, "main", tmp_path / "clone")
    ran = _gitlab(clone, tmp_path, CI_COMMIT_BRANCH="main", STUB_EXIT=str(code))
    assert ran.code == code


# --- GitHub ---------------------------------------------------------------------------------------
def _action(clone: Path, tmp_path: Path, **env: str) -> tuple[Ran, dict[str, str]]:
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    variables = {
        "GITHUB_WORKSPACE": str(clone),
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_SHA": _git(clone, "rev-parse", "HEAD"),
        "EVENT_NAME": "push",
        "PR_BASE_SHA": "",
        "MERGE_GROUP_BASE_SHA": "",
        "INPUT_VERSION": "1.2.3",
        "INPUT_BASE_URL": "",
        "INPUT_API_KEY": "key",
        "INPUT_PROJECT": "",
        "INPUT_CHECK_CONFIG": "",
        "INPUT_DIRECTORY": ".",
        "INPUT_BASELINE": BASELINE,
        "INPUT_GATE": "true",
        "INPUT_STRICT": "false",
        "INPUT_PASS_ON_OUTAGE": "true",
        "INPUT_SARIF": "true",
        "INPUT_ARGS": "",
        "INPUT_CATEGORY": "",
        **env,
    }
    ran = _run("bash", ACTION_SCRIPT, clone, tmp_path, "uvx", variables)
    outputs = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if line
    )
    return ran, outputs


@pytest.fixture
def merge_ref(origin: Path) -> str:
    """The merge commit GitHub makes for a pull request: main first, the head second."""
    _git(origin, "checkout", "-q", "--detach", "main")
    _git(origin, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "update-ref", "refs/pull/1/merge", sha)
    _git(origin, "checkout", "-q", "main")
    return sha


@needs_git
def test_action_pull_request_is_gated_on_the_merge_commits_first_parent(
    origin: Path, merge_ref: str, tmp_path: Path
) -> None:
    clone = _clone(origin, merge_ref, tmp_path / "clone")
    ran, outputs = _action(
        clone,
        tmp_path,
        EVENT_NAME="pull_request",
        GITHUB_SHA=merge_ref,
        INPUT_BASE_URL="https://tripl.example.com",
        INPUT_API_KEY="key",
    )
    assert ran.code == 0, ran.log
    assert ran.argv[0] == "tripl@1.2.3"
    assert ran.after("--new-from-rev") == _git(origin, "rev-parse", "main")
    assert ran.after("--baseline") == f"{clone}/{BASELINE}"
    sarif = ran.after("--report").removeprefix("sarif=")
    assert outputs == {"exit-code": "0", "sarif": sarif, "category": "tripl-check"}
    assert ran.env == ["https://tripl.example.com", "key"]


@needs_git
def test_action_pull_request_head_checked_out_is_gated_on_its_merge_base(
    origin: Path, merge_ref: str, tmp_path: Path
) -> None:
    # actions/checkout with ref: the pull request's head, not the merge commit.
    # main moved on after the branch: its tip is not what the branch changed.
    fork_point = _git(origin, "rev-parse", "main")
    (origin / "app" / "a.swift").write_text("a, edited on main\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "main moves on")
    tip = _git(origin, "rev-parse", "main")
    clone = _clone(origin, "feature", tmp_path / "clone")
    ran, _ = _action(
        clone, tmp_path, EVENT_NAME="pull_request", GITHUB_SHA=merge_ref, PR_BASE_SHA=tip
    )
    assert ran.code == 0, ran.log
    assert ran.after("--new-from-rev") == fork_point


@needs_git
@pytest.mark.parametrize(
    ("directory", "category", "expected"),
    [
        ("apps/ios/", "", "tripl-check/apps/ios"),
        ("./apps/android", "", "tripl-check/apps/android"),
        ("apps/ios", "ios", "ios"),
    ],
)
def test_action_gives_each_app_its_own_code_scanning_category(
    origin: Path, tmp_path: Path, directory: str, category: str, expected: str
) -> None:
    # Two steps of one job uploading under one category would collide.
    clone = _clone(origin, "main", tmp_path / "clone")
    (clone / "apps" / "ios").mkdir(parents=True)
    (clone / "apps" / "android").mkdir(parents=True)
    ran, outputs = _action(clone, tmp_path, INPUT_DIRECTORY=directory, INPUT_CATEGORY=category)
    assert ran.code == 0, ran.log
    assert outputs["category"] == expected


@needs_git
def test_action_skips_a_pull_request_without_a_key(origin: Path, tmp_path: Path) -> None:
    # A fork's pull request, or Dependabot's, gets no secrets: skipped, not red.
    clone = _clone(origin, "feature", tmp_path / "clone")
    ran, outputs = _action(clone, tmp_path, EVENT_NAME="pull_request", INPUT_API_KEY="")
    assert ran.code == 0
    assert not ran.called
    assert "not checked" in ran.log
    assert outputs == {}
    # Anywhere else a missing key is the CLI's to refuse (exit 2).
    ran, _ = _action(clone, tmp_path, EVENT_NAME="push", INPUT_API_KEY="")
    assert ran.called


@needs_git
def test_action_says_why_when_the_base_cannot_be_fetched(
    origin: Path, merge_ref: str, tmp_path: Path
) -> None:
    clone = _clone(origin, merge_ref, tmp_path / "clone")
    _git(clone, "remote", "set-url", "origin", (tmp_path / "gone").as_uri())
    ran, _ = _action(clone, tmp_path, EVENT_NAME="pull_request", GITHUB_SHA=merge_ref)
    assert ran.code == 2
    assert not ran.called
    assert "fetch-depth: 0" in ran.log


@needs_git
def test_action_merge_group_is_gated_on_its_base(origin: Path, tmp_path: Path) -> None:
    base = _git(origin, "rev-parse", "main")
    clone = _clone(origin, "feature", tmp_path / "clone")
    ran, _ = _action(clone, tmp_path, EVENT_NAME="merge_group", MERGE_GROUP_BASE_SHA=base)
    assert ran.code == 0, ran.log
    assert ran.after("--new-from-rev") == base


@needs_git
def test_action_push_has_no_gate_base(origin: Path, tmp_path: Path) -> None:
    clone = _clone(origin, "main", tmp_path / "clone")
    ran, _ = _action(clone, tmp_path, INPUT_GATE="true", INPUT_SARIF="false")
    assert ran.code == 0, ran.log
    assert ran.after("--new-from-rev") == ""
    assert "--report" not in ran.argv


@needs_git
@pytest.mark.parametrize(
    ("code", "pass_on_outage", "step"),
    [(0, "true", 0), (1, "true", 1), (2, "true", 2), (4, "true", 0), (4, "false", 4)],
)
def test_action_step_exits_as_the_check_did_unless_tripl_was_down(
    origin: Path, tmp_path: Path, code: int, pass_on_outage: str, step: int
) -> None:
    clone = _clone(origin, "main", tmp_path / "clone")
    ran, outputs = _action(
        clone, tmp_path, STUB_EXIT=str(code), INPUT_PASS_ON_OUTAGE=pass_on_outage
    )
    assert ran.code == step
    assert outputs["exit-code"] == str(code)
    assert ("nothing was checked" in ran.log) == (code == 4 and pass_on_outage == "true")


@needs_git
def test_action_refuses_a_version_that_is_not_a_release(origin: Path, tmp_path: Path) -> None:
    clone = _clone(origin, "main", tmp_path / "clone")
    ran, _ = _action(clone, tmp_path, INPUT_VERSION="1.2.3; rm -rf /")
    assert ran.code == 2
    assert not ran.called
