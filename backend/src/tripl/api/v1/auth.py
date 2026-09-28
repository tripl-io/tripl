import logging
import smtplib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import CurrentUserDep, SessionDep
from tripl.auth_utils import hash_session_token
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.middleware.rate_limit import (
    enforce,
    login_rate_limiter,
    register_rate_limiter,
    status_rate_limiter,
    verify_email_rate_limiter,
)
from tripl.models.domain_enums import OrganizationRole
from tripl.models.invitation import Invitation
from tripl.models.user import User
from tripl.schemas.auth import (
    PASSWORD_MAX_LENGTH,
    AuthStatusResponse,
    AuthUserResponse,
    LoginRequest,
    RegisterRequest,
    VerifyEmailConfirmRequest,
    validate_password_strength,
)
from tripl.schemas.invitation import InvitationAcceptRequest, InvitationPreview
from tripl.services import (
    app_settings_service,
    audit_service,
    auth_service,
    email_verification_service,
    invitation_service,
)

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


def _send_verification_email(
    *,
    recipient: str,
    verify_link: str,
    email_config: app_settings_service.EmailConfig,
) -> None:
    """Send the email-verification link through the operator's relay.

    A ``BackgroundTask`` like :func:`_send_password_reset_email`, and just as
    best-effort: a failure is logged and swallowed — the account exists and
    can ask for another link (``POST /auth/verify-email/request``).
    """
    from_address = email_config.smtp_from_address
    if not from_address:
        logger.warning("Verification email not sent: SMTP_FROM_ADDRESS is unset")
        return

    from tripl.worker.tasks.alerts_channels import _send_email_message

    body = (
        "Confirm the email address of your tripl account.\n\n"
        f"Open this link to verify it (valid for "
        f"{email_verification_service.EMAIL_VERIFICATION_TTL_HOURS} hours):\n"
        f"{verify_link}\n\n"
        "If you did not create a tripl account, you can ignore this email.\n"
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
            subject="Verify your tripl email address",
            body=body,
        )
    except Exception:  # noqa: BLE001 — best-effort; the user can resend.
        logger.exception("Failed to send verification email")


async def _operator_mail(
    session: AsyncSession,
) -> tuple[app_settings_service.EmailConfig, str]:
    """The operator relay's email config and ``app_base_url``.

    Account mail always goes through the OPERATOR's relay (F20 PR9), never an
    organization's.
    """
    overrides = await app_settings_service.get_service_overrides(session)
    return (
        app_settings_service.build_email_config(overrides),
        app_settings_service.build_runtime_config(overrides).app_base_url,
    )


def _queue_verification_email(
    background_tasks: BackgroundTasks,
    *,
    user: User,
    raw_token: str,
    email_config: app_settings_service.EmailConfig,
    app_base_url: str,
) -> None:
    background_tasks.add_task(
        _send_verification_email,
        recipient=user.email,
        verify_link=email_verification_service.build_verification_link(app_base_url, raw_token),
        email_config=email_config,
    )


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
    # A hosted instance has no first-user bootstrap, so it reports ``has_users``
    # true without counting: no first-account note, and an unauthenticated
    # caller cannot learn whether the service is empty.
    hosted = settings.deployment_mode == DEPLOYMENT_HOSTED
    has_users = True if hosted else await auth_service.has_any_users(session)
    overrides = await app_settings_service.get_service_overrides(session)
    return AuthStatusResponse(
        has_users=has_users,
        registration_enabled=await auth_service.is_registration_allowed(
            session, is_first_user=not has_users
        ),
        email_configured=app_settings_service.email_can_send(
            app_settings_service.build_email_config(overrides)
        ),
        deployment_mode=settings.deployment_mode,
        email_verification_required=email_verification_service.verification_required(),
    )


@router.post(
    "/register",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce(register_rate_limiter))],
)
async def register(
    response: Response,
    session: SessionDep,
    data: RegisterRequest,
    background_tasks: BackgroundTasks,
) -> AuthUserResponse:
    """Self-service sign-up.

    Self-hosted: into the default organization (the first account owns it and
    is a platform admin); ``org_name`` / ``org_slug`` are ignored.

    Hosted: ``org_name`` and ``org_slug`` are required and the account creates
    and owns that organization. It starts unverified — the verification link
    is mailed through the operator relay after the commit (a failed send is
    logged; the user can resend) — so 503 up front when that relay cannot send.
    """
    if settings.deployment_mode != DEPLOYMENT_HOSTED:
        user, session_token = await auth_service.register_user(session, data)
        _set_session_cookie(response, session_token)
        return await auth_service.build_auth_user_response(session, user)

    email_config, app_base_url = await _operator_mail(session)
    user, session_token, raw_token = await auth_service.register_hosted_user(
        session, data, email_can_send=app_settings_service.email_can_send(email_config)
    )
    _queue_verification_email(
        background_tasks,
        user=user,
        raw_token=raw_token,
        email_config=email_config,
        app_base_url=app_base_url,
    )
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
        # The organization role the invitee joins with.
        role=OrganizationRole(invitation.org_role),
        expires_at=invitation.expires_at,
    )


@router.post(
    "/invitations/{token}/accept",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce(register_rate_limiter))],
)
async def accept_invitation(
    request: Request,
    response: Response,
    session: SessionDep,
    token: str,
    data: InvitationAcceptRequest,
    background_tasks: BackgroundTasks,
) -> AuthUserResponse:
    """Redeem an invitation: into a new account, or into the signed-in one.

    Signed in (a browser session cookie): the invitation adds a membership of
    its organization to THIS account, but only when the account's email is the
    invitation's (case-insensitive) — else 403 and the invitation stays unused;
    on a hosted instance the account must also have verified its address (403);
    409 when the account is already a member. Answers 200 and leaves the
    session as it is (F20 PR6).

    Not signed in: the new-account path. ``password`` is required, the account
    is created with the invitation's address and the new user is signed
    straight in (201). Self-hosted the account counts as email-verified. Hosted
    it starts unverified — the inviter was handed the raw link, so redeeming it
    proves nothing about the address — and a verification link is mailed
    through the operator relay after the commit (a failed send is logged; the
    user can resend).
    Reachable regardless of ``registration_mode`` —
    an owner-issued, single-use, expiring, address-bound invitation is a
    different mechanism from the instance-wide door, so a closed instance can
    still onboard exactly the people its owner named.

    On the register rate-limit bucket, so guessing tokens costs the same as
    hammering signup.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    signed_in = await auth_service.get_user_by_session_token(session, cookie) if cookie else None
    if signed_in is not None:
        try:
            invitation = await invitation_service.accept_as_signed_in(
                session, raw_token=token, user=signed_in
            )
        except invitation_service.InvitationEmailMismatchError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "This invitation was sent to a different email address. Sign in "
                    "with that address to accept it."
                ),
            ) from None
        except invitation_service.EmailNotVerifiedError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Verify your email address before accepting an invitation.",
            ) from None
        except invitation_service.AlreadyMemberError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You are already a member of this organization.",
            ) from None
        await _record_acceptance(session, signed_in, invitation, existing_account=True)
        response.status_code = status.HTTP_200_OK
        return await auth_service.build_auth_user_response(session, signed_in)

    if data.password is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A password is required to create the account.",
        )
    user, session_token, invitation = await invitation_service.redeem_invitation(
        session, raw_token=token, password=data.password, name=data.name
    )
    await _record_acceptance(session, user, invitation, existing_account=False)
    if email_verification_service.verification_required():
        await _mail_verification_link(session, background_tasks, user)
    _set_session_cookie(response, session_token)
    return await auth_service.build_auth_user_response(session, user)


async def _mail_verification_link(
    session: AsyncSession, background_tasks: BackgroundTasks, user: User
) -> None:
    """Issue ``user`` a verification token, commit, and queue the mail.

    For an account that already exists and is signed in (an invitation just
    redeemed into it). When the operator relay cannot send, nothing is issued:
    a warning is logged and the user can ask again once it can.
    """
    email_config, app_base_url = await _operator_mail(session)
    if not app_settings_service.email_can_send(email_config):
        logger.warning("Verification email not sent: the operator email relay cannot send")
        return
    raw_token = await email_verification_service.issue_token(session, user)
    await session.commit()
    _queue_verification_email(
        background_tasks,
        user=user,
        raw_token=raw_token,
        email_config=email_config,
        app_base_url=app_base_url,
    )


async def _record_acceptance(
    session: AsyncSession, user: User, invitation: Invitation, *, existing_account: bool
) -> None:
    """``user.invite_accept``, filed in the invitation's organization. Commits."""
    await audit_service.record(
        session,
        user=user,
        action="user.invite_accept",
        target_type="invitation",
        target_id=invitation.id,
        target_name=invitation.email,
        payload={
            "role": invitation.org_role,
            "existing_account": existing_account,
        },
        organization_id=invitation.organization_id,
    )


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
    # Account mail always goes through the OPERATOR's relay (F20 PR9): the
    # operator-scope overrides below, never an organization's.
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


@router.post(
    "/verify-email/request",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(enforce(verify_email_rate_limiter))],
)
async def request_email_verification(
    request: Request,
    session: SessionDep,
    current_user: CurrentUserDep,
    background_tasks: BackgroundTasks,
) -> None:
    """Mail the signed-in account a fresh verification link (a resend).

    A browser session only (an API key is 403). 204 without doing anything
    when the address is already verified or the instance does not require
    verification (self-hosted); 503 when the operator relay cannot send.
    Otherwise every earlier link of the account stops working and the new one
    goes out after the response (a failed send is logged).
    """
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="A browser session is required"
        )
    if (
        not email_verification_service.verification_required()
        or current_user.email_verified_at is not None
    ):
        return
    email_config, app_base_url = await _operator_mail(session)
    if not app_settings_service.email_can_send(email_config):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=auth_service.EMAIL_DELIVERY_NOT_CONFIGURED_MESSAGE,
        )
    raw_token = await email_verification_service.issue_token(session, current_user)
    await session.commit()
    _queue_verification_email(
        background_tasks,
        user=current_user,
        raw_token=raw_token,
        email_config=email_config,
        app_base_url=app_base_url,
    )


@router.post(
    "/verify-email/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(enforce(login_rate_limiter))],
)
async def confirm_email_verification(
    request: Request, session: SessionDep, data: VerifyEmailConfirmRequest
) -> None:
    """Redeem a verification link, signed in as the account it was issued to.

    Needs a browser session (401 without one): the link alone proves only that
    someone read the mail, the session proves it is the account holder who
    did. A session of a different account gets the same 400 as an unknown,
    expired or used token, and the token stays usable. On success every other
    session of the account is signed out, and on a hosted instance an address
    listed in ``PLATFORM_ADMIN_EMAILS`` becomes a platform admin — the only
    place that grant happens. On the login bucket, like the password reset
    confirm, so guessing tokens costs what guessing passwords does.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    signed_in = await auth_service.get_user_by_session_token(session, cookie) if cookie else None
    if cookie is None or signed_in is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to confirm your email address.",
        )
    await email_verification_service.confirm(
        session,
        data.token,
        session_user=signed_in,
        session_token_hash=hash_session_token(cookie),
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
async def get_me(
    request: Request, session: SessionDep, current_user: CurrentUserDep
) -> AuthUserResponse:
    """The signed-in account with its organization role(s) and the platform-admin flag.

    For an API key it also names the key's organization (``org``) and scope
    (``api_key_scope``): what ``tripl whoami`` prints. Read from the database on
    every call, so a membership added, changed or removed shows at once.
    """
    return await auth_service.build_auth_user_response(
        session, current_user, api_key_scope=getattr(request.state, "api_key_scope", None)
    )
