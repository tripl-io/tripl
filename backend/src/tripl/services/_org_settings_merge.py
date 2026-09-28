"""How an organization's own settings combine with the operator's (F20 PR9-PR11).

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
  back to the operator, whatever the fallback policy says — except a group's
  switch (:data:`GROUP_SWITCHES`, the search-embeddings switch), which is off
  while the policy withholds its group.
* The **ceiling** fields protect worker memory and the process: an organization
  may lower them, never raise them above the operator's effective value.
* The **narrowing** fields are allow-lists (F20 PR11, critique #15): the
  organization's list is intersected with the operator's, so it can drop a
  content type the operator allows but never add one (SVG, HTML).
* The storage group (F20 PR11) is a credential group that the fallback policy
  never withholds: an organization without storage of its own keeps the
  operator's (under its own ``orgs/{id}/`` key prefix), because photos off is
  not a safe default the way "AI off" is — nothing of the operator's is sent to
  a server the organization chose.
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
#: The search-embedding endpoint (F20 PR10): where indexed plan text is sent,
#: the key it is sent with, and the provider/model that define the vector space
#: (``embedding_service.embedding_provenance``). One unit for the same reason as
#: the AI endpoint: an organization that points embeddings at its own server
#: never has the operator's key sent there.
EMBEDDING_GROUP: tuple[str, ...] = (
    "search_embedding_provider",
    "search_embedding_model",
    "search_embedding_base_url",
    "search_embedding_api_key",
)
#: Where an organization's photo blobs are written (F20 PR11): the backend, the
#: bucket, the service-account JSON that writes to it and how URLs are made.
#: One unit: an organization naming its own bucket never has the operator's
#: credentials (the server's credential file or its ambient identity) used on
#: it. The server paths (``photo_local_dir``, ``gcs_photo_credentials_path``)
#: are operator-only and not part of it (critique #12).
STORAGE_GROUP: tuple[str, ...] = (
    "photo_storage_backend",
    "gcs_photo_bucket",
    "gcs_photo_credentials_json",
    "gcs_photo_public",
    "gcs_photo_signed_url_ttl_seconds",
)
CREDENTIAL_GROUPS: tuple[tuple[str, ...], ...] = (
    AI_ENDPOINT_GROUP,
    SMTP_GROUP,
    EMBEDDING_GROUP,
    STORAGE_GROUP,
)
#: Groups inherited whole whatever ``ORG_SETTINGS_OPERATOR_FALLBACK`` says.
POLICY_EXEMPT_GROUPS: frozenset[tuple[str, ...]] = frozenset({STORAGE_GROUP})

#: A group's on/off switch, forced off when the fallback policy withholds the
#: group: an organization with no endpoint of its own under
#: ``ORG_SETTINGS_OPERATOR_FALLBACK=none`` has semantic search OFF (lexical
#: search is unaffected), rather than "on" with no endpoint — which would leave
#: every new search document ``pending`` for a worker that can never embed it.
GROUP_SWITCHES: dict[tuple[str, ...], str] = {EMBEDDING_GROUP: "search_embeddings_enabled"}

#: Org values for these are clamped to the operator's effective value.
CEILING_FIELDS: tuple[str, ...] = (
    "ai_timeout_seconds",
    "ai_max_output_tokens",
    "scan_row_limit_default",
    "metrics_row_limit_default",
    "photo_max_size_mb",
)

#: Comma-separated allow-lists: an organization's value is narrowed to the
#: operator's (F20 PR11). Matching is case-insensitive.
NARROWING_FIELDS: tuple[str, ...] = ("photo_allowed_mime",)

#: Hosts an organization can point tripl at. An organization-set value (every
#: organization scope, hosted or self-hosted) is refused when it is (or resolves
#: to) a private address, at save and at use.
GUARDED_HOST_FIELDS: tuple[str, ...] = ("ai_base_url", "smtp_host", "search_embedding_base_url")

#: Blanked, not defaulted, when a group is disabled by policy: the addresses.
_DISABLED_BLANK_FIELDS = frozenset(
    {
        "ai_base_url",
        "smtp_host",
        "smtp_username",
        "smtp_from_address",
        "search_embedding_base_url",
    }
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


def split_list(value: Any) -> list[str]:
    """``"a, B,,c"`` -> ``["a", "b", "c"]``: a comma-separated allow-list, normalised."""
    return [item.strip().lower() for item in str(value or "").split(",") if item.strip()]


def narrow_list(org_value: Any, operator_value: Any) -> str:
    """The organization's list, keeping only what the operator's list allows."""
    allowed = set(split_list(operator_value))
    kept: list[str] = []
    for item in split_list(org_value):
        if item in allowed and item not in kept:
            kept.append(item)
    return ",".join(kept)


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
        elif inherit_groups or group in POLICY_EXEMPT_GROUPS:
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

    for group, switch in GROUP_SWITCHES.items():
        if switch in org_fields and any(provenance.get(f) == "disabled" for f in group):
            values[switch] = False
            provenance[switch] = "disabled"

    for field in CEILING_FIELDS:
        if provenance.get(field) == "org":
            values[field] = min(int(values[field]), int(base[field]))

    for field in NARROWING_FIELDS:
        if provenance.get(field) == "org":
            values[field] = narrow_list(values[field], base[field])

    org_hosts = frozenset(
        field for field in GUARDED_HOST_FIELDS if provenance.get(field) == "org" and values[field]
    )
    return OrgMerge(values=values, provenance=provenance, org_hosts=org_hosts)
