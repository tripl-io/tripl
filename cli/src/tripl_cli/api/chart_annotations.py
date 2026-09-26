"""Chart annotations: the markers charts draw at a point in time.

Only the create route has a builder. ``tripl annotate`` is the one caller, and it
exists for CI: a deploy step posts "Deployed web 2026.09.25" with a link to the
release, and every monitoring (Volume tab) chart in the project shows it.

The route answers **201** for a new row and **200** when it de-duplicated: the
same label posted with source ``api`` to the same project within the last 24
hours returns the row that already exists instead of creating a second one.
Only ``api`` is de-duplicated here (``release`` is unique per label forever, and
is worker-only); ``manual`` creates are never de-duplicated and always 201. A retried CI job is
therefore safe to re-run, and the status code is the only way to tell the two
answers apart - the bodies have the same shape.
"""

from __future__ import annotations

from datetime import datetime

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import JsonDict, to_rfc3339

CREATE = "/projects/{slug}/annotations"

ENDPOINTS: tuple[tuple[str, str], ...] = (("post", CREATE),)

# `ChartAnnotationScopeType` verbatim. A scoped annotation only shows on charts
# of that scope; a project-level one (no scope) shows on every monitoring
# (Volume tab) chart in the project.
SCOPE_TYPES: tuple[str, ...] = ("project_total", "event_type", "event", "metric")

# What a client may send as `source`. `manual` is the app's own default and
# `release` is reserved to the metrics worker (the API answers 422), so the CLI
# always sends `api`.
SOURCE_API = "api"

# `ChartAnnotationCreate`'s own bounds, so a typo costs no request.
LABEL_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000
SCOPE_REF_MAX_LENGTH = 120
URL_MAX_LENGTH = 500

# 201 is a new annotation; 200 is the one that already existed.
STATUS_CREATED = 201
STATUS_DEDUPLICATED = 200


def create_annotation(
    slug: str,
    *,
    label: str,
    at: datetime,
    url: str | None = None,
    description: str | None = None,
    scope_type: str | None = None,
    scope_ref: str | None = None,
) -> ApiRequest:
    """``POST /projects/{slug}/annotations`` with ``source="api"``.

    Editor-gated: a ``tk_w_`` key backed by an editor or owner. ``bucket`` is the
    route's name for the timestamp. Unset members are omitted rather than sent
    as null, so the server's defaults (the colour among them) apply.
    """
    body: JsonDict = {"label": label, "bucket": to_rfc3339(at), "source": SOURCE_API}
    if url is not None:
        body["url"] = url
    if description is not None:
        body["description"] = description
    if scope_type is not None:
        body["scope_type"] = scope_type
    if scope_ref is not None:
        body["scope_ref"] = scope_ref
    return ApiRequest("POST", CREATE.format(slug=slug), json_body=body)
