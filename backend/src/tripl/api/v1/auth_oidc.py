"""Sign in through the instance's OpenID Connect provider (``OIDC_*``).

``start`` and the provider's ``callback`` (``_instance_login_routes``).
"""

from __future__ import annotations

from tripl.api.v1._instance_login_routes import build_router
from tripl.services import oidc_login_service

STATE_COOKIE = "tripl_oidc_state"

router = build_router(
    prefix="/auth/oidc",
    state_cookie=STATE_COOKIE,
    start_login=oidc_login_service.start,
    finish_login=oidc_login_service.callback,
    start_description="Send the browser to the instance's OpenID Connect provider.",
    callback_description=(
        "The provider's redirect back: signed in and into the app, or back to sign-in."
    ),
)
