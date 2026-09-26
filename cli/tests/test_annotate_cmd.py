"""``tripl annotate`` end to end, through ``main([...])`` against a fake instance.

The case worth the most is the 200: the API de-duplicates a label posted twice
within a day and answers with the row that already exists. A retried deploy job
must exit 0 and must SAY it created nothing, or an operator chasing a missing
marker is told one was just drawn.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import httpx
import pytest

from tripl_cli.cli import main

from .conftest import API_KEY, BASE_URL, FakeInstance, make_annotation


def _document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload


def _posts(tripl_api: FakeInstance) -> list[httpx.Request]:
    return [call.request for call in tripl_api.router.calls if call.request.method == "POST"]


def _body(request: httpx.Request) -> dict[str, Any]:
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


def test_annotate_posts_source_api_with_the_label_url_and_timestamp(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.annotate("prod")
    argv = [
        "annotate",
        "Deployed web 2026.09.25",
        "--project",
        "prod",
        "--url",
        "https://github.com/acme/web/releases/tag/2026.09.25",
        "--at",
        "2026-09-25T14:02:00Z",
    ]
    assert main(argv) == 0
    posts = _posts(tripl_api)
    assert [request.url.path for request in posts] == ["/api/v1/projects/prod/annotations"]
    assert _body(posts[0]) == {
        "label": "Deployed web 2026.09.25",
        "bucket": "2026-09-25T14:02:00Z",
        "source": "api",
        "url": "https://github.com/acme/web/releases/tag/2026.09.25",
    }
    out = capsys.readouterr().out
    assert "prod: annotated 'Deployed web 2026.09.25' at 2026-09-25T14:02:00Z (ann-1)." in out


def test_annotate_without_at_sends_the_current_time(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """``bucket`` is required by the route, so "now" is sent, never omitted."""
    tripl_api.annotate("prod")
    assert main(["annotate", "Deployed", "--project", "prod"]) == 0
    body = _body(_posts(tripl_api)[0])
    assert body["bucket"].endswith("Z")
    assert set(body) == {"label", "bucket", "source"}


def test_a_naive_at_is_read_as_utc(tripl_api: FakeInstance, configured_env: None) -> None:
    tripl_api.annotate("prod")
    assert main(["annotate", "x", "--project", "prod", "--at", "2026-09-25T14:02:00"]) == 0
    assert _body(_posts(tripl_api)[0])["bucket"] == "2026-09-25T14:02:00Z"


def test_a_200_is_reported_as_de_duplicated_and_still_exits_zero(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.annotate("prod", make_annotation(), status=200)
    assert main(["annotate", "Deployed web 2026.09.25", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "already exists (ann-1" in out
    assert "de-duplicated" in out
    assert "annotated '" not in out


def test_the_json_document_says_whether_it_de_duplicated(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.annotate("prod")
    assert main(["annotate", "Deployed", "--project", "prod", "--json"]) == 0
    created = _document(capsys)
    tripl_api.annotate("prod", make_annotation(), status=200)
    assert main(["annotate", "Deployed", "--project", "prod", "--json"]) == 0
    repeated = _document(capsys)

    assert created["command"] == "annotate"
    assert created["deduplicated"] is False
    assert repeated["deduplicated"] is True
    assert created["result"]["id"] == "ann-1"
    assert created["scan"] is None and created["drift_id"] is None


def test_every_mutation_document_carries_the_deduplicated_key(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One shape for the whole write surface: null where it does not apply."""
    tripl_api.scan_run("prod", "scan-1")
    assert main(["scans", "run", "scan-1", "--project", "prod", "--json"]) == 0
    scans = _document(capsys)
    tripl_api.annotate("prod")
    assert main(["annotate", "Deployed", "--project", "prod", "--json"]) == 0
    annotate = _document(capsys)
    assert scans.keys() == annotate.keys()
    assert scans["deduplicated"] is None


def test_dry_run_sends_nothing_and_prints_the_request(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.annotate("prod")
    argv = ["annotate", "Deployed", "--project", "prod", "--at", "2026-09-25T14:02:00Z"]
    assert main([*argv, "--dry-run", "--json"]) == 0
    assert not _posts(tripl_api)
    document = _document(capsys)
    assert document["dry_run"] is True
    assert document["result"] is None
    assert document["deduplicated"] is None
    assert document["requests"] == 0
    assert document["request"] == {
        "method": "POST",
        "path": "/projects/prod/annotations",
        "params": {},
        "body": {"label": "Deployed", "bucket": "2026-09-25T14:02:00Z", "source": "api"},
    }


def test_scope_and_description_are_sent_when_given(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    tripl_api.annotate("prod")
    argv = [
        "annotate",
        "Checkout rewrite",
        "--project",
        "prod",
        "--scope-type",
        "event_type",
        "--scope-ref",
        "et-1",
        "--description",
        "new payment sheet",
    ]
    assert main(argv) == 0
    body = _body(_posts(tripl_api)[0])
    assert body["scope_type"] == "event_type"
    assert body["scope_ref"] == "et-1"
    assert body["description"] == "new payment sheet"


@pytest.mark.parametrize(
    "extra",
    [
        ["--scope-type", "event_type"],
        ["--scope-ref", "et-1"],
    ],
)
def test_half_a_scope_exits_usage_without_a_request(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    assert main(["annotate", "x", "--project", "prod", *extra]) == 2
    assert not _posts(tripl_api)
    assert "--scope-type and --scope-ref go together" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["annotate", "x", "--project", "prod", "--url", "github.com/acme/web"],
        ["annotate", "x", "--project", "prod", "--url", "javascript:alert(1)"],
        ["annotate", "x", "--project", "prod", "--url", "https://e.x/" + "a" * 500],
        ["annotate", "x", "--project", "prod", "--at", "yesterday"],
        ["annotate", "x", "--project", "prod", "--scope-type", "dashboard", "--scope-ref", "d"],
        ["annotate", "   ", "--project", "prod"],
        ["annotate", "x" * 201, "--project", "prod"],
        ["annotate", "x"],
        ["annotate", "x", "--project", "prod", "--project", "stage"],
        ["annotate", "x", "--project", "prod", "--yes"],
    ],
)
def test_bad_arguments_exit_usage_without_a_request(
    tripl_api: FakeInstance,
    configured_env: None,
    argv: list[str],
) -> None:
    """Every refusal costs no request. ``--yes`` included: there is no prompt."""
    try:
        code = main(argv)
    except SystemExit as exc:  # argparse's own usage exit
        code = int(exc.code or 0)
    assert code == 2
    assert not _posts(tripl_api)


def test_a_read_only_key_surfaces_the_403_guidance(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No local tk_r_/tk_w_ gate: the server is the authority and it says why."""
    tripl_api.annotate("prod", {"detail": "API key has read-only scope"}, status=403)
    assert main(["annotate", "Deployed", "--project", "prod"]) == 1
    assert "tk_r_ keys cannot write" in capsys.readouterr().err


def test_a_422_is_passed_through_verbatim(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.annotate(
        "prod",
        {"detail": [{"loc": ["body", "url"], "msg": "URL scheme should be 'http' or 'https'"}]},
        status=422,
    )
    assert main(["annotate", "Deployed", "--project", "prod"]) == 1
    assert "rejected the request (422)" in capsys.readouterr().err


def _option(parser: argparse.ArgumentParser, flag: str) -> argparse.Action | None:
    return next((action for action in parser._actions if flag in action.option_strings), None)


def test_url_is_the_link_here_and_the_instance_everywhere_else() -> None:
    """``annotate --url`` is the release link; no other command lost its ``--url``.

    argparse shares action objects between a parent and its children, so the
    tempting ``conflict_handler="resolve"`` would have stripped ``--url`` from the
    shared connection action - from every command at once. This walks the real
    parser to prove it did not happen.
    """
    from tripl_cli.cli import build_parser

    parser = build_parser()
    commands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    annotate = commands.choices["annotate"]
    link = _option(annotate, "--url")
    instance = _option(annotate, "--base-url")
    assert link is not None and link.dest == "link"
    assert instance is not None and instance.dest == "url"
    for name in ("doctor", "status", "watch"):
        option = _option(commands.choices[name], "--url")
        assert option is not None and option.dest == "url", name
    root = _option(parser, "--url")
    assert root is not None and root.dest == "url"


@pytest.mark.parametrize(
    "argv",
    [
        ["--url", BASE_URL, "annotate", "Deployed", "--project", "prod"],
        ["annotate", "Deployed", "--project", "prod", "--base-url", BASE_URL],
    ],
)
def test_the_instance_url_still_reaches_annotate_by_both_routes(
    tripl_api: FakeInstance,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setenv("TRIPL_API_KEY", API_KEY)
    tripl_api.annotate("prod")
    assert main(argv) == 0
    assert len(_posts(tripl_api)) == 1
