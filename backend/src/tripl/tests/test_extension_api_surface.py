"""The Community names the Enterprise package relies on, pinned here.

``tripl-enterprise`` does not reach the core only through the hooks in
``tripl.extensions``: it imports Community modules directly, and many of the
names it uses look private (a leading underscore, an underscore module). It
pins a Community commit (``COMMUNITY_REF``) and its nightly CI runs against
Community main, so a rename here would otherwise surface only there, a day
later. This test makes it surface in Community CI.

Each name below is one Enterprise imports, reaches as a module attribute, or
replaces in its own tests (its suite reuses this package's fixtures and
helpers). Renaming one is fine: keep the old name as an alias or change
Enterprise in step, then update this list. A name Enterprise stops using
leaves the list in the same change as the Enterprise one.
"""

from __future__ import annotations

import importlib

import pytest

_SERVER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tripl.api.deps", ("_UNVERIFIED_ALLOWED_PREFIX", "_app_path")),
    ("tripl.api.v1._auth_redirects", ("app_base_url", "error_redirect")),
    ("tripl.api.v1.auth", ("_set_session_cookie",)),
    ("tripl.services._celery_dispatch", ("dispatch",)),
    ("tripl.services.demo_service", ("_new_demo_project", "_purge_audit_trail")),
    ("tripl.worker.db", ("_get_sync_session",)),
    # The escalation's destination step: the demo zero-egress check, then the
    # plain send the destination Test button uses.
    ("tripl.worker.tasks.alerts", ("_assert_egress_allowed",)),
    ("tripl.worker.tasks.alerts_plain", ("ChannelTarget", "PlainMessage", "send_plain_message")),
    ("tripl.worker.tasks.alerts_teams", ("build_teams_plain_message",)),
    (
        "tripl.worker.tasks.alerts_pagerduty",
        ("PAGERDUTY_SOURCE", "PAGERDUTY_SUMMARY_MAX_CHARS", "first_app_link", "severity_of"),
    ),
    # An organization's SSO sign-in builds and finishes its OIDC flow with these.
    ("tripl.services.oidc.flow", ("authorization_url", "pkce_challenge")),
    ("tripl.services.oidc.id_tokens", ("authenticate",)),
    # Enterprise builds the limiters of its own routes (SSO setup, audit, SCIM).
    (
        "tripl.middleware.rate_limit",
        (
            "SSO_RATE_LIMIT_PER_MINUTE",
            "allow",
            "enforce",
            "limiter_for",
            "reset_rate_limiters",
            "retry_after_for_key",
            "sso_rate_limiter",
        ),
    ),
)

# What Enterprise's tests replace (monkeypatch) or call.
_TEST_SEAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tripl.api.v1.auth", ("_send_password_reset_email",)),
    ("tripl.crypto", ("_fernet",)),
    ("tripl.extensions", ("_load",)),
    ("tripl.services.auth_service", ("_get_user_by_email",)),
    ("tripl.services.demo_service", ("_seed_demo_content",)),
    ("tripl.services.oidc.idp_http", ("_send",)),
    ("tripl.services.safe_http", ("_WatchedHTTPSHandler",)),
    ("tripl.worker.tasks._demo_pause", ("is_demo_paused",)),
    ("tripl.worker.tasks.alerts", ("_send_email_message", "_send_slack_message")),
)

# This package's test helpers, which Enterprise's suite imports.
_TEST_HELPERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "tripl.tests._incident_summary_seed",
        ("BUCKET", "SeededIncident", "add_item", "seed_incident"),
    ),
    ("tripl.tests._members", ("add_member", "add_member_by_slug", "add_org_member")),
    (
        "tripl.tests._platform_world",
        (
            "API",
            "GLOBEX",
            "GLOBEX_ID",
            "PASSWORD",
            "SHOP",
            "World",
            "audit_rows",
            "build_world",
            "new_client",
            "register",
            "set_org_status",
        ),
    ),
    ("tripl.tests._tenancy", ("use_multi_tenant",)),
    (
        "tripl.tests._verification_mail",
        (
            "API",
            "PASSWORD",
            "confirm",
            "hosted_sign_up",
            "install_mail_sink",
            "new_client",
            "token_from",
        ),
    ),
    ("tripl.tests.conftest", ("TestSessionLocal", "engine")),
    ("tripl.tests.test_google_sign_in", ("_landed", "_sign_in", "_user", "google")),
    ("tripl.tests.test_rbac", ("iter_api_routes",)),
)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (module, name)
        for module, names in (*_SERVER, *_TEST_SEAMS, *_TEST_HELPERS)
        for name in names
    ],
)
def test_a_name_enterprise_relies_on_is_still_there(module: str, name: str) -> None:
    assert hasattr(importlib.import_module(module), name), (
        f"{module}.{name} is gone, and the Enterprise package uses it: keep an alias "
        "or change Enterprise in step (see this file's docstring)."
    )
