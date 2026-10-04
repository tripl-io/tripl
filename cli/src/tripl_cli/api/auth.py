"""Who the key is.

``/auth/me`` doubles as the oracle for a key's REACH: ``deps._enforce_project_scope``
rejects any route without a ``slug`` path parameter for a project-bound key, so a
200 means instance-wide and a 403 means fenced to one project.
"""

from __future__ import annotations

from tripl_cli.api.request import ApiRequest

ME = "/auth/me"

# The UNAUTHENTICATED bootstrap signal: {has_users, registration_enabled}. It is
# what lets `tripl install` end with next steps that are true statements about
# the instance it just started instead of a static blurb — "create the first
# account, it becomes the owner" versus "this instance already has accounts"
# versus "registration is closed, ask an owner for an invitation". The auth
# screen reads it for the same reason.
STATUS = "/auth/status"

ENDPOINTS: tuple[tuple[str, str], ...] = (("get", ME), ("get", STATUS))


def get_me() -> ApiRequest:
    return ApiRequest("GET", ME)


def get_status() -> ApiRequest:
    return ApiRequest("GET", STATUS)
