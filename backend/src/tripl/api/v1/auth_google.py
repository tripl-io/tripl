"""Sign in with Google: ``start`` and Google's ``callback`` (``_instance_login_routes``)."""

from __future__ import annotations

from tripl.api.v1._instance_login_routes import build_router
from tripl.services import google_login_service

STATE_COOKIE = "tripl_google_state"

router = build_router(
    prefix="/auth/google",
    state_cookie=STATE_COOKIE,
    start_login=google_login_service.start,
    finish_login=google_login_service.callback,
    start_description="Send the browser to Google's account chooser.",
    callback_description="Google's redirect back: signed in and into the app, or back to sign-in.",
)
