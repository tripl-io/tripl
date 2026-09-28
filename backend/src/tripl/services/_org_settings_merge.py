"""How an organization's own settings combine with the operator's (F20 PR9).

Pure: no database, no ``settings`` singleton. ``app_settings_service`` reads the
two override documents and hands this module the operator's EFFECTIVE values
(env with the operator's overrides on top) plus the organization's decrypted
overrides; this decides, field by field, what the organization runs with.

The rules (design section 5, critique #13 and #15):

* A field the organization set is the organization's.
* A field it did not set is inherited from the operator — except inside a
  **credential group**. An endpoint and the credential it is sent to are one
  unit: an organization that sets ``ai_base_url`` (or ``smtp_host``) without its
  own key must never have the operator's key sent to ITS server. So when an
  organization sets ANY field of a group, the whole group is the organization's
  and an unset member takes its built-in default (a secret: empty), never the
  operator's value. A group the organization did not touch is inherited as a
  whole, or — under ``ORG_SETTINGS_OPERATOR_FALLBACK=none`` — not at all, which
  leaves the feature off for that organization.
* Non-secret scalars (row limits, timeouts, prompts, the AI switch) always fall
  back to the operator, whatever the fallback policy says.
* The **ceiling** fields protect worker memory and the process: an organization
  may lower them, never raise them above the operator's effective value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

#: The AI chat endpoint and what authenticates against it. ``ai_model`` is part
#: of it: a model name only means something on the endpoint that serves it.
AI_ENDPOINT_GROUP: tuple[str, ...] = ("ai_base_url", "ai_api_key", "ai_model")
#: The SMTP relay, its credentials and the sender it accepts.
SMTP_GROUP: tuple[str, ...] = (
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "smtp_password",
    "smtp_from_address",
)
CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (AI_ENDPOINT_GROUP, SMTP_GROUP)

#: Org values for these are clamped to the operator's effective value.
CEILING_FIELDS: tuple[str, ...] = (
    "ai_timeout_seconds",
    "ai_max_output_tokens",
    "scan_row_limit_default",
    "metrics_row_limit_default",
)

#: Hosts an organization can point tripl at. An organization-set value (every
#: organization scope, hosted or self-hosted) is refused when it is (or resolves
#: to) a private address, at save and at use.
GUARDED_HOST_FIELDS: tuple[str, ...] = ("ai_base_url", "smtp_host")

#: Blanked, not defaulted, when a group is disabled by policy: the addresses.
_DISABLED_BLANK_FIELDS = frozenset(
    {"ai_base_url", "smtp_host", "smtp_username", "smtp_from_address"}
)

#: Where an organization's value came from, before it is mapped onto the
#: public ``SettingSource`` (which also names the operator's own sources).
Provenance = Literal["org", "inherited", "group_default", "disabled"]


@dataclass(frozen=True)
class OrgMerge:
    values: dict[str, Any]
    provenance: dict[str, Provenance]
    #: The guarded host fields whose value the ORGANIZATION supplied.
    org_hosts: frozenset[str]


def _blank(field: str, secret_fields: frozenset[str], code_default: Callable[[str], Any]) -> Any:
    if field in secret_fields:
        return ""
    return code_default(field)


def merge_org_values(
    base: Mapping[str, Any],
    org_values: Mapping[str, Any],
    *,
    org_fields: frozenset[str],
    secret_fields: frozenset[str],
    inherit_groups: bool,
    code_default: Callable[[str], Any],
) -> OrgMerge:
    """The organization's effective values; see the module docstring for the rules.

    ``base`` is the operator's effective value of EVERY field; ``org_values``
    holds only what the organization stored, already decrypted and restricted
    to ``org_fields``. ``code_default`` returns a field's built-in default.
    """
    values = dict(base)
    provenance: dict[str, Provenance] = {}
    grouped: set[str] = set()

    for group in CREDENTIAL_GROUPS:
        members = [field for field in group if field in org_fields]
        grouped.update(members)
        if any(field in org_values for field in members):
            for field in members:
                if field in org_values:
                    values[field] = org_values[field]
                    provenance[field] = "org"
                else:
                    values[field] = _blank(field, secret_fields, code_default)
                    provenance[field] = "group_default"
        elif inherit_groups:
            for field in members:
                provenance[field] = "inherited"
        else:
            for field in members:
                # The address blank as well as the secret: an endpoint with no
                # credential is still somewhere tripl would talk to.
                values[field] = (
                    ""
                    if field in _DISABLED_BLANK_FIELDS
                    else _blank(field, secret_fields, code_default)
                )
                provenance[field] = "disabled"

    for field in org_fields - grouped:
        if field in org_values:
            values[field] = org_values[field]
            provenance[field] = "org"
        else:
            provenance[field] = "inherited"

    for field in CEILING_FIELDS:
        if provenance.get(field) == "org":
            values[field] = min(int(values[field]), int(base[field]))

    org_hosts = frozenset(
        field for field in GUARDED_HOST_FIELDS if provenance.get(field) == "org" and values[field]
    )
    return OrgMerge(values=values, provenance=provenance, org_hosts=org_hosts)
