"""Uploading a ``tripl check`` run (``tripl check --upload``).

A WRITE, unlike validation: it records the run in tripl. A ``write`` key of a
project editor may send it, and so may the narrow ``check_upload`` key a CI job
holds (``tk_c_``), which reads like a ``read`` key and may write nothing else.
A run on the repository's default branch becomes what "used in code" shows on
each event; any other run is kept as a summary for the history.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tripl_cli.api.request import ApiRequest

UPLOAD = "/projects/{slug}/check-runs"

ENDPOINTS: tuple[tuple[str, str], ...] = (("post", UPLOAD),)

# The route's own cap on the calls of one run.
MAX_CALLS = 5000

# The route's per-call bounds (``CheckRunCallIn``, ``CheckRunFinding``). The
# CLI cuts a longer value rather than have one call fail the whole upload.
PATH_MAX_LENGTH = 1000
EVENT_TYPE_MAX_LENGTH = 100
NAME_MAX_LENGTH = 500
MESSAGE_MAX_LENGTH = 2000
FIELD_MAX_LENGTH = 255
CODE_MAX_LENGTH = 100
MAX_FINDINGS_PER_CALL = 50
# Of the run itself.
REPOSITORY_MAX_LENGTH = 255
REF_MAX_LENGTH = 255
URL_MAX_LENGTH = 2000


def upload(slug: str, run: Mapping[str, Any]) -> ApiRequest:
    """One run: ``repository``, ``commit_sha``, ``ref``, ``default_branch``, ``calls``, ...

    ``run["calls"]`` is sent as given, so it must already be at most ``MAX_CALLS``.
    """
    calls = run.get("calls") or ()
    if len(calls) > MAX_CALLS:
        raise ValueError(f"at most {MAX_CALLS} calls per check run, got {len(calls)}")
    return ApiRequest("POST", UPLOAD.format(slug=slug), json_body=dict(run))
