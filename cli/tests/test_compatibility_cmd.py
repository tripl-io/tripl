import json
from pathlib import Path

import pytest

from tripl_cli.cli import main

from .conftest import FakeInstance, make_branch


def seed(api: FakeInstance, event_branch: str) -> None:
    api.branches(
        "shop",
        [make_branch("main-id", name="main", kind="main"), make_branch("work-id", name="rollout")],
    )
    mapping = (Path(__file__).parents[1] / "examples/compatibility/orders.json").read_text()
    api.event(
        "shop",
        "evt",
        {
            "id": "evt",
            "branch_id": event_branch,
            "name": "orders",
            "meta_values": [{"meta_field_definition_id": "m", "value": mapping}],
        },
    )
    api._respond(
        api.events_url("shop").rsplit("/", 1)[0] + "/meta-fields",
        200,
        [{"id": "m", "name": "compatibility_mapping", "field_type": "string"}],
    )


@pytest.mark.parametrize(
    ("verb", "branch", "extra"),
    [
        ("model", "main-id", []),
        (
            "preview",
            "work-id",
            ["--branch", "rollout", "--start", "2026-05-01", "--end", "2026-05-08"],
        ),
    ],
)
def test_compile(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    branch: str,
    extra: list[str],
) -> None:
    seed(tripl_api, branch)
    assert main(["compatibility", verb, "evt", "--project", "shop", "--json", *extra]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["branch_id"] == branch
    assert result["event_id"] == "evt"
    assert "SELECT" in result["sql"]


def test_refuse_wrong_branch(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(tripl_api, "work-id")
    assert main(["compatibility", "model", "evt", "--project", "shop"]) == 2
    assert "branch" in capsys.readouterr().err
