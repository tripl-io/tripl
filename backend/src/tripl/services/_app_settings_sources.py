"""Resolution of the runtime settings: effective values and where each came from.

Private half of :mod:`tripl.services.app_settings_service`, which re-exports
every name here; import from the facade.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from tripl.config import SMTP_SECURITY_NONE, SMTP_SECURITY_STARTTLS, Settings, settings
from tripl.services._app_settings_ai import default_ai_prompts
from tripl.services._app_settings_core import _org_values, build_service_values
from tripl.services._app_settings_fields import (
    EDITABLE_FIELDS,
    ORG_FIELDS,
    REPORTED_FIELDS,
    SECRET_FIELDS,
    SettingSource,
)
from tripl.services._org_settings_merge import merge_org_values


@dataclass(frozen=True)
class ResolvedSettings:
    """Every editable field's effective value, and where each one came from."""

    values: dict[str, Any]
    sources: dict[str, SettingSource]
    #: ``None`` for the operator view; else the scope that was resolved.
    org_scope: uuid.UUID | None
    #: The raw overrides of the scope being viewed (operator or organization).
    overridden_fields: tuple[str, ...]
    #: Guarded host fields whose value an ORGANIZATION supplied (every organization
    #: scope, any deployment mode); checked against private addresses at use time.
    guarded_hosts: frozenset[str]
    #: The operator's effective SMTP host (operator override -> env), kept on an
    #: organization view so an org that types the operator's own host is not
    #: mistaken for one with its own relay (critique #16).
    operator_smtp_host: str = ""


def resolve_settings(
    operator_overrides: dict[str, Any],
    org_overrides: dict[str, Any] | None,
    *,
    org_scope: uuid.UUID | None = None,
) -> ResolvedSettings:
    """Pure resolution: operator view when ``org_overrides`` is ``None``, else the org's.

    The organization view applies org override -> operator override -> env with
    the rules of :mod:`tripl.services._org_settings_merge`.
    """
    base = build_service_values(operator_overrides)
    base_sources: dict[str, SettingSource] = {
        field: _setting_source(field, base[field], operator_overrides) for field in REPORTED_FIELDS
    }
    if org_overrides is None:
        return ResolvedSettings(
            values=base,
            sources=base_sources,
            org_scope=None,
            overridden_fields=tuple(sorted(k for k in operator_overrides if k in EDITABLE_FIELDS)),
            guarded_hosts=frozenset(),
        )
    merged = merge_org_values(
        base,
        _org_values(org_overrides),
        org_fields=ORG_FIELDS,
        secret_fields=SECRET_FIELDS,
        inherit_groups=settings.org_settings_operator_fallback == "all",
        code_default=_group_default,
    )
    sources = dict(base_sources)
    for field, provenance in merged.provenance.items():
        if provenance == "org":
            sources[field] = "org"
        elif provenance == "group_default":
            sources[field] = "default"
        elif provenance == "disabled":
            sources[field] = "disabled"
    return ResolvedSettings(
        values=merged.values,
        sources=sources,
        org_scope=org_scope,
        overridden_fields=tuple(sorted(k for k in org_overrides if k in ORG_FIELDS)),
        # Every organization scope (F20 PR9): on a self-hosted instance the
        # operator's own team is the default organization, which resolves as
        # the operator view above and is never guarded.
        guarded_hosts=merged.org_hosts,
        operator_smtp_host=str(base.get("smtp_host") or ""),
    )


def _group_default(field: str) -> Any:
    default = _code_default(field)
    return "" if default is _NO_DEFAULT else default


_NO_DEFAULT = object()


# The three system prompts are not ``Settings`` fields at all — env_service_values
# reads them straight off ai_defaults — so no environment variable can deliver
# them and their built-in constant IS the default to compare against.
_PROMPT_DEFAULTS = default_ai_prompts()


# Fields whose REPORTED value is derived from more than one ``Settings`` field,
# so the field's own class default is not what an untouched instance shows.
# ``smtp_security`` defaults to "" meaning "ask the deprecated smtp_use_tls",
# and what reaches the operator is the answer, never the empty string — so
# comparing against "" would badge a fresh instance "Env" and credit a delivery
# that never happened. Derived from the sibling's CLASS default rather than
# written out, so the two cannot drift apart.
_DERIVED_DEFAULTS: dict[str, Any] = {
    "smtp_security": (
        SMTP_SECURITY_STARTTLS
        if Settings.model_fields["smtp_use_tls"].get_default()
        else SMTP_SECURITY_NONE
    ),
    # Not a ``Settings`` field at all: nothing but an organization delivers it.
    "gcs_photo_credentials_json": "",
}


def _code_default(field: str) -> Any:
    """What this field holds when nothing — no env var, no .env line — delivered it."""
    if field in _DERIVED_DEFAULTS:
        return _DERIVED_DEFAULTS[field]
    # ``model_fields`` is read off the CLASS: instance access is deprecated in
    # pydantic 2.11+ and this project pins 2.13.
    info = Settings.model_fields.get(field)
    if info is not None and not info.is_required():
        return info.get_default(call_default_factory=True)
    return _PROMPT_DEFAULTS.get(field, _NO_DEFAULT)


def _setting_source(field: str, value: Any, overrides: dict[str, Any]) -> SettingSource:
    """Where the value in front of the operator actually came from.

    Note the honest limit of comparing against the built-in default: an operator
    who sets an environment variable to EXACTLY that default is reported as
    "default", because from here the two are indistinguishable. That is the safe
    direction — this never claims a delivery that did not happen, it only
    declines to credit a redundant variable — and the UI's badge says so in as
    many words rather than implying "default" proves nothing arrived.

    A normalising validator can fold a delivered value onto the default the same
    way (``LOG_LEVEL=info`` -> ``"INFO"``), with the same consequence.

    ``override`` is checked first on purpose: an override whose value happens to
    equal the default still reads "Override", because a row exists and Reset
    will clear it.
    """
    if field in overrides:
        return "override"
    default = _code_default(field)
    # ``type(value) is type(default)`` keeps Python's ``False == 0`` from matching
    # a bool field against an int default.
    if default is not _NO_DEFAULT and type(value) is type(default) and value == default:
        return "default"
    return "env"
