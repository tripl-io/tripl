"""Worker import and send-time guard regressions from batch 08."""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tripl.worker.tasks.alerts import _assert_rule_active, _telegram_too_long_error
from tripl.worker.tasks.implementation_tickets import _is_transient_tracker_error


def test_alerts_can_be_imported_first_in_a_fresh_process() -> None:
    code = (
        "from tripl.worker.tasks.alerts import send_alert_delivery; "
        "from tripl.worker.tasks import metrics; "
        "assert metrics.send_alert_delivery is send_alert_delivery"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1200:]


def test_send_guard_refuses_disabled_or_muted_rule() -> None:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    rule = SimpleNamespace(name="High volume", enabled=False, muted_until=None)
    with pytest.raises(ValueError, match="disabled"):
        _assert_rule_active(rule, now=now)  # type: ignore[arg-type]

    rule.enabled = True
    rule.muted_until = now + timedelta(hours=1)
    with pytest.raises(ValueError, match="muted"):
        _assert_rule_active(rule, now=now)  # type: ignore[arg-type]

    rule.muted_until = now - timedelta(minutes=1)
    _assert_rule_active(rule, now=now)  # type: ignore[arg-type]


def test_telegram_too_long_error_retains_sent_progress() -> None:
    delivery = SimpleNamespace(items=[object(), object()])
    error = _telegram_too_long_error(
        delivery,  # type: ignore[arg-type]
        {uuid.uuid4()},
        1,
        ValueError("Bad Request: message is too long"),
    )
    assert "1 of 2 items" in str(error)
    assert "Shorten the rule's templates" in str(error)


def test_tracker_retry_classifies_transport_and_server_failures() -> None:
    server_error = urllib.error.HTTPError("https://jira.example", 503, "Unavailable", {}, None)
    client_error = urllib.error.HTTPError("https://jira.example", 400, "Bad request", {}, None)
    assert _is_transient_tracker_error(server_error)
    assert _is_transient_tracker_error(urllib.error.URLError("DNS timeout"))
    assert not _is_transient_tracker_error(client_error)

    try:
        raise ValueError("Jira issue creation failed") from server_error
    except ValueError as wrapped:
        assert _is_transient_tracker_error(wrapped)
