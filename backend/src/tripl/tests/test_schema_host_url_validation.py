"""Format-only validation for owner-configurable hosts and URLs.

These guards intentionally do NOT block private/loopback/link-local addresses:
data sources legitimately point at private DBs and ai_base_url at local LLMs.
They only reject values that are clearly malformed (a URL where a bare host is
expected, or a non-http(s) / hostless URL).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tripl.models.data_source import DBType
from tripl.schemas.app_settings import AiSettingsUpdate
from tripl.schemas.data_source import DataSourceCreate, DataSourceUpdate


def _make_source(host: str) -> DataSourceCreate:
    return DataSourceCreate(
        name="ds",
        db_type=DBType.clickhouse,
        host=host,
        database_name="db",
    )


@pytest.mark.parametrize(
    "host",
    ["localhost", "10.0.0.5", "169.254.169.254", "db.internal.vpc", "[::1]", "  db.example.com  "],
)
def test_data_source_host_allows_bare_hosts_including_private(host: str) -> None:
    # Private/loopback/metadata addresses are allowed by design (no IP blocking).
    src = _make_source(host)
    assert src.host == host.strip()


@pytest.mark.parametrize(
    "host",
    ["http://evil.example.com", "db.example.com/path", "db host", "https://10.0.0.5"],
)
def test_data_source_host_rejects_malformed(host: str) -> None:
    with pytest.raises(ValidationError):
        _make_source(host)


def test_data_source_update_host_optional_but_format_checked() -> None:
    # Left UNSET, not sent as null: ``host`` is a NOT NULL column, so an explicit
    # null is refused by ``_reject_explicit_nulls``. What this
    # line pins is that the field is optional, which is what "optional but format
    # checked" means for a PATCH.
    assert DataSourceUpdate().host is None
    assert DataSourceUpdate(host="10.1.2.3").host == "10.1.2.3"
    with pytest.raises(ValidationError):
        DataSourceUpdate(host="http://x/y")


@pytest.mark.parametrize(
    "url",
    ["http://localhost:11434", "https://api.openai.com/v1", "http://10.0.0.9:8000/v1"],
)
def test_ai_base_url_allows_http_and_local(url: str) -> None:
    # http and localhost/private hosts are allowed for self-hosted/local LLMs.
    assert AiSettingsUpdate(ai_base_url=url).ai_base_url == url


@pytest.mark.parametrize("url", ["not-a-url", "ftp://example.com", "://nohost", "example.com"])
def test_ai_base_url_rejects_non_http_or_hostless(url: str) -> None:
    with pytest.raises(ValidationError):
        AiSettingsUpdate(ai_base_url=url)


def test_ai_base_url_none_and_empty_pass_through() -> None:
    assert AiSettingsUpdate(ai_base_url=None).ai_base_url is None
    assert AiSettingsUpdate(ai_base_url="").ai_base_url == ""
