import logging
import smtplib

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from tripl.api.deps import CurrentUserDep, SessionDep
from tripl.config import settings
from tripl.middleware.rate_limit import (
    enforce,
    login_rate_limiter,
    register_rate_limiter,
    status_rate_limiter,
)
from tripl.models.domain_enums import OrganizationRole
from tripl.schemas.auth import (
    PASSWORD_MAX_LENGTH,
    AuthStatusResponse,
    AuthUserResponse,
    LoginRequest,
    RegisterRequest,
    validate_password_strength,
)
from tripl.schemas.invitation import InvitationAcceptRequest, InvitationPreview
from tripl.services import app_settings_service, auth_service, invitation_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        httponly=True,
        max_age=settings.session_ttl_hours * 60 * 60,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


class PasswordResetRequestBody(BaseModel):
    # EmailStr normalizes the address; a syntactically invalid email is rejected
    # at the boundary (422) before any lookup, same as register.
    email: EmailStr


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def _enforce_password_policy(cls, value: str) -> str:
        # Same shared policy as register — enforced here at the schema boundary
        # so a weak new password never reaches the service.
        return validate_password_strength(value)


class PasswordResetRequestResponse(BaseModel):
    # Neutral, account-agnostic. ``email_configured`` is an instance-wide fact
    # (identical for every caller regardless of the submitted address), so it
    # lets the UI pick the right copy without enabling per-address enumeration.
    message: str
    email_configured: bool


class PasswordResetConfirmResponse(BaseModel):
    message: str


def _build_reset_link(app_base_url: str, raw_token: str) -> str:
    # The SPA reads ``?reset_token=`` off the /auth route and switches to reset
    # mode (see AuthPage). ``app_base_url`` should be set whenever email is
    # configured; if blank, the link degrades to a relative path.
    return f"{app_base_url.rstrip('/')}/auth?reset_token={raw_token}"


def _send_password_reset_email(
    *,
    recipient: str,
    reset_link: str,
    email_config: app_settings_service.EmailConfig,
) -> None:
    """Send the reset email via the shared alert email sender.

    Runs as a FastAPI ``BackgroundTask`` (after the response is sent), so a slow
    or failing SMTP round-trip neither blocks the request nor becomes a timing
    oracle for whether the account exists. Reuses the alert channel's
    ``_send_email_message`` instead of opening a second SMTP client. Best-effort:
    any failure is logged and swallowed since the caller already returned the
    same neutral response.
    """
    from_address = email_config.smtp_from_address
    if not from_address:
        logger.warning("Password reset email not sent: SMTP_FROM_ADDRESS is unset")
        return

    # Lazy import keeps the worker email module off the API's import path.
    from tripl.worker.tasks.alerts_channels import _send_email_message

    body = (
        "We received a request to reset the password for your tripl account.\n\n"
        f"Use this link to choose a new password (valid for "
        f"{auth_service.PASSWORD_RESET_TTL_HOURS} hour):\n"
        f"{reset_link}\n\n"
        "If you did not request this, you can safely ignore this email — your "
        "password will not change.\n"
    )
    try:
        _send_email_message(
            smtp_module=smtplib,
            smtp_host=email_config.smtp_host,
            smtp_port=email_config.smtp_port,
            smtp_username=email_config.smtp_username,
            smtp_password=email_config.smtp_password,
            smtp_security=email_config.smtp_security,
            from_address=from_address,
            recipients=[recipient],
            subject="Reset your tripl password",
            body=body,
        )
    except Exception:  # noqa: BLE001 — best-effort; never surface to the caller.
        logger.exception("Failed to send password reset email")


@router.get(
    "/status",
    response_model=AuthStatusResponse,
    dependencies=[Depends(enforce(status_rate_limiter))],
)
async def get_status(session: SessionDep) -> AuthStatusResponse:
    # Unauthenticated on purpose: the login/register screen queries this before
    # anyone is signed in to decide whether to show the first-account note and
    # whether a sign-up form is worth rendering at all.
    # Rate limited on its own bucket (never shares login/register quota) so an
    # unauthenticated caller can't hammer the COUNT(*) behind it.
    has_users = await auth_service.has_any_users(session)
    overrides = await app_settings_service.get_service_overrides(session)
    return AuthStatusResponse(
        has_users=has_users,
        registration_enabled=await auth_service.is_registration_allowed(
            session, is_first_user=not has_users
        ),
        email_configured=app_settings_service.email_can_send(
            app_settings_service.build_email_config(overrides)
        ),
    )


@router.post(
    "/register",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce(register_rate_limiter))],
)
async def register(
    response: Response, session: SessionDep, data: RegisterRequest
) -> AuthUserResponse:
    user, session_token = await auth_service.register_user(session, data)
    _set_session_cookie(response, session_token)
    return await auth_service.build_auth_user_response(session, user)


@router.get(
    "/invitations/{token}",
    response_model=InvitationPreview,
    dependencies=[Depends(enforce(status_rate_limiter))],
)
async def preview_invitation(session: SessionDep, token: str) -> InvitationPreview:
    """Show who an invitation is for, before the invitee has an account.

    Unauthenticated by necessity — the whole point is that this person cannot
    sign in yet. It discloses nothing the token holder does not already have:
    the address it was issued to, the organization role it grants, and when it lapses. It
    does not reveal whether the instance has other users, or who they are.

    Shares the cheap /status bucket rather than the register bucket: previewing
    is a read, and it must not consume the quota the invitee needs to actually
    redeem. Unknown, expired and used tokens all return the same 400.
    """
    invitation = await invitation_service.get_valid_invitation(session, token)
    return InvitationPreview(
        email=invitation.email,
        # The organization role the invitee joins with; NULL only for a row
        # written behind the application's back, which redeems as member.
        role=OrganizationRole(invitation.org_role or OrganizationRole.member.value),
        expires_at=invitation.expires_at,
    )


@router.post(
    "/invitations/{token}/accept",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce(register_rate_limiter))],
)
async def accept_invitation(
    response: Response, session: SessionDep, token: str, data: InvitationAcceptRequest
) -> AuthUserResponse:
    """Redeem an invitation into an account, and sign the new user straight in.

    Reachable regardless of ``registration_mode`` — that is the entire point:
    an owner-issued, single-use, expiring, address-bound invitation is a
    different mechanism from the instance-wide door, so a closed instance can
    still onboard exactly the people its owner named.

    On the register rate-limit bucket, so guessing tokens costs the same as
    hammering signup.
    """
    user, session_token = await invitation_service.redeem_invitation(
        session, raw_token=token, password=data.password, name=data.name
    )
    _set_session_cookie(response, session_token)
    return await auth_service.build_auth_user_response(session, user)


@router.post(
    "/login",
    response_model=AuthUserResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(enforce(login_rate_limiter))],
)
async def login(response: Response, session: SessionDep, data: LoginRequest) -> AuthUserResponse:
    user, session_token = await auth_service.authenticate_user(session, data)
    _set_session_cookie(response, session_token)
    return await auth_service.build_auth_user_response(session, user)


@router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestResponse,
    dependencies=[Depends(enforce(login_rate_limiter))],
)
async def request_password_reset(
    session: SessionDep,
    data: PasswordResetRequestBody,
    background_tasks: BackgroundTasks,
) -> PasswordResetRequestResponse:
    # Always 200 with a neutral message so the response never reveals whether the
    # address is registered. A token is minted and emailed ONLY when the instance
    # has SMTP configured AND a matching account exists; otherwise nothing is
    # stored or sent. ``email_configured`` is instance-wide, so returning it
    # (for the UI's fallback copy) does not enable enumeration.
    overrides = await app_settings_service.get_service_overrides(session)
    email_config = app_settings_service.build_email_config(overrides)
    # The flag has to mean "this can actually send"; see ``email_can_send``.
    email_configured = app_settings_service.email_can_send(email_config)

    if email_configured:
        issued = await auth_service.request_password_reset(session, data.email)
        if issued is not None:
            user, raw_token = issued
            reset_link = _build_reset_link(
                app_settings_service.build_runtime_config(overrides).app_base_url,
                raw_token,
            )
            background_tasks.add_task(
                _send_password_reset_email,
                recipient=user.email,
                reset_link=reset_link,
                email_config=email_config,
            )

    return PasswordResetRequestResponse(
        message=auth_service.PASSWORD_RESET_NEUTRAL_MESSAGE,
        email_configured=email_configured,
    )


@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetConfirmResponse,
    dependencies=[Depends(enforce(login_rate_limiter))],
)
async def confirm_password_reset(
    session: SessionDep, data: PasswordResetConfirmBody
) -> PasswordResetConfirmResponse:
    # Password strength is enforced on PasswordResetConfirmBody (422); the service
    # validates the token (invalid/expired/used → 400) and applies the change.
    await auth_service.confirm_password_reset(session, data.token, data.new_password)
    return PasswordResetConfirmResponse(
        message="Your password has been reset. You can now sign in with your new password."
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> None:
    del current_user
    await auth_service.logout_session(session, request.cookies.get(settings.session_cookie_name))
    _clear_session_cookie(response)


@router.get("/me", response_model=AuthUserResponse)
async def get_me(session: SessionDep, current_user: CurrentUserDep) -> AuthUserResponse:
    """The signed-in account with its organization role(s) and the platform-admin flag."""
    return await auth_service.build_auth_user_response(session, current_user)
