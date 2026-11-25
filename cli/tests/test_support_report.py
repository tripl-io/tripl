"""``tripl doctor --support-report PATH``: what goes in, and what never does.

The privacy tests plant sentinel secrets everywhere doctor can see them — the
API key, the instance URL, a scan config name, a failing job's error text with
SQL and a password in it — and then assert that none of them reaches the file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import API_KEY, BASE_URL, FakeInstance, make_job, make_scan_config
from tripl_cli.cli import main
from tripl_cli.model import CHECK_IDS, Check, Finding, Instance, Report, Severity
from tripl_cli.support_report import (
    OTHER_CATEGORY,
    SUPPORT_REPORT_VERSION,
    build_support_report,
    safe_version,
)

pytestmark = pytest.mark.usefixtures("configured_env")

SECRET_SQL = "SELECT card_number FROM sentinel_payments WHERE password = 'hunter2-SENTINEL'"
SECRET_NAME = "SENTINEL-internal-billing-scan"


def plant_failing_scan(api: FakeInstance, now: datetime) -> None:
    api.scans("prod", [make_scan_config(name=SECRET_NAME)])
    api.jobs(
        "prod",
        "scan-1",
        [
            make_job(
                job_id=f"job-SENTINEL-{index}",
                status="failed",
                at=now - timedelta(hours=index + 1),
                error_message=SECRET_SQL,
            )
            for index in range(7)
        ],
    )


def read_report(path: Path) -> tuple[str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return text, json.loads(text)


def test_report_names_versions_checks_and_categories(
    tripl_api: FakeInstance, tmp_path: Path, now: datetime
) -> None:
    tripl_api.health(payload={"status": "ok", "version": "0.3.3"})
    plant_failing_scan(tripl_api, now)
    destination = tmp_path / "support.json"

    code = main(["doctor", "--support-report", str(destination)])

    assert code == 3
    _, report = read_report(destination)
    assert report["kind"] == "tripl-support-report"
    assert report["support_report_version"] == SUPPORT_REPORT_VERSION
    assert report["server"] == {"version": "0.3.3"}
    assert report["client"]["tool"] == "tripl"
    assert report["exit_code"] == 3
    assert [check["id"] for check in report["checks"]] == list(CHECK_IDS)
    assert report["checks_not_run"] == []
    scans = next(check for check in report["checks"] if check["id"] == "scans")
    assert scans["status"] == "fail"
    codes = {category["code"] for category in scans["error_categories"]}
    assert "scan_config_failing" in codes


def test_no_sentinel_secret_reaches_the_file(
    tripl_api: FakeInstance, tmp_path: Path, now: datetime
) -> None:
    plant_failing_scan(tripl_api, now)
    destination = tmp_path / "support.json"

    main(["doctor", "--support-report", str(destination)])

    text, _ = read_report(destination)
    for secret in (
        API_KEY,
        BASE_URL,
        "tripl.test",
        "SENTINEL",
        "hunter2",
        "card_number",
        "SELECT",
        '"prod"',
        "scan-1",
        "ds-1",
        "Bearer",
    ):
        assert secret not in text, f"{secret!r} leaked into the support report"


def test_report_is_written_when_the_instance_is_unreachable(
    tripl_api: FakeInstance, tmp_path: Path
) -> None:
    tripl_api.health_raises(httpx.ConnectError(f"connection refused to {BASE_URL}"))
    destination = tmp_path / "support.json"

    code = main(["doctor", "--support-report", str(destination)])

    assert code == 3
    text, report = read_report(destination)
    assert report["server"] == {"version": None}
    assert report["checks_not_run"] == ["auth", "projects", "data_sources", "scans", "drifts"]
    connectivity = report["checks"][0]
    assert connectivity == {
        "id": "connectivity",
        "status": "fail",
        "could_run": True,
        "error_categories": [{"code": "instance_unreachable", "count": 1}],
    }
    assert "connection refused" not in text


def test_an_existing_file_is_never_overwritten(tripl_api: FakeInstance, tmp_path: Path) -> None:
    destination = tmp_path / "support.json"
    destination.write_text("operator notes", encoding="utf-8")

    code = main(["doctor", "--support-report", str(destination)])

    assert code == 2
    assert destination.read_text(encoding="utf-8") == "operator notes"


def test_a_missing_directory_is_a_usage_error(tripl_api: FakeInstance, tmp_path: Path) -> None:
    code = main(["doctor", "--support-report", str(tmp_path / "nope" / "support.json")])

    assert code == 2


def test_json_stdout_stays_one_document(
    tripl_api: FakeInstance, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "support.json"

    main(["doctor", "--json", "--support-report", str(destination)])

    captured = capsys.readouterr()
    assert len(captured.out.strip().splitlines()) == 1
    assert json.loads(captured.out)["command"] == "doctor"
    assert "support report written" in captured.err
    assert destination.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.3.3", "0.3.3"),
        ("0.4.0rc1", "0.4.0rc1"),
        ("0.0.0+unknown", "0.0.0+unknown"),
        ("<html>502 Bad Gateway</html>", None),
        ("https://internal.example/", None),
        (3, None),
        (None, None),
    ],
)
def test_server_version_is_accepted_only_when_it_looks_like_one(
    value: object, expected: str | None
) -> None:
    assert safe_version(value) == expected


def test_a_finding_code_that_is_not_an_identifier_becomes_other() -> None:
    report = Report(
        command="doctor",
        instance=Instance(
            base_url=BASE_URL,
            base_url_source="$TRIPL_BASE_URL",
            api_key_source="$TRIPL_API_KEY",
            api_key_scope="SENTINEL-scope",
        ),
        generated_at=datetime(2026, 11, 25, tzinfo=UTC),
        duration_ms=5,
        requests=3,
        checks=(
            Check(
                id="scans",
                title="Scans",
                status=Severity.FAIL,
                summary=f"failing at {BASE_URL}",
                findings=(
                    Finding(code="Error: password=x", severity=Severity.FAIL, message=SECRET_SQL),
                ),
            ),
            Check(id="not_a_known_check", title="x", status=Severity.FAIL, summary="x"),
        ),
    )

    document = build_support_report(report, exit_code=3, server_version=None)

    assert document["api_key_scope"] == "unknown"
    assert [check["id"] for check in document["checks"]] == ["scans"]
    assert document["checks"][0]["error_categories"] == [{"code": OTHER_CATEGORY, "count": 1}]
    text = json.dumps(document)
    assert "SENTINEL" not in text
    assert "password" not in text
    assert BASE_URL not in text
