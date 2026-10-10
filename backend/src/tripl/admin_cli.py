"""``tripl-admin``: the operator's shell tool (F20 PR14).

How a hosted operator bootstraps the first platform admin (critique #25): the
flag is otherwise granted only by another platform admin from the console, and
a hosted sign-up never gets it. And how anyone gets back into an account when
the instance cannot send email and nobody who could hand them a reset link can
sign in: the first owner of a fresh install, say. Run inside the server image
or any environment with the server's ``DATABASE_URL``/``SYNC_DATABASE_URL``::

    tripl-admin grant-platform-admin ops@example.com
    tripl-admin revoke-platform-admin ops@example.com
    tripl-admin list-platform-admins
    tripl-admin password-reset-link ada@example.com

Every change is written to the audit log at platform scope, with no user (the
shell is not an account) and ``"via": "tripl-admin"``. The last platform admin
cannot be revoked. A reset link is the emailed reset's own (single use, the
same expiry, it replaces any earlier link), printed on ``APP_BASE_URL``
(or its override in Settings › Platform › Runtime), and the account's
password keeps working until it is used. Exit codes: 0 done (or nothing to do), 1 no
such account, 2 refused (last admin) or a usage error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC
from typing import TextIO

from sqlalchemy.orm import Session

from tripl.auth_utils import normalize_email
from tripl.services import app_settings_service, password_reset_links
from tripl.services import platform_admins as console

EXIT_OK = 0
EXIT_UNKNOWN_USER = 1
EXIT_REFUSED = 2


def grant(session: Session, email: str, out: TextIO) -> int:
    try:
        change = console.set_platform_admin_sync(session, normalize_email(email), grant=True)
    except console.PlatformUserNotFoundError:
        print(f"No account with email {email}", file=sys.stderr)
        return EXIT_UNKNOWN_USER
    if change.changed:
        print(f"Granted platform admin to {change.user.email}", file=out)
    else:
        print(f"{change.user.email} is already a platform admin", file=out)
    return EXIT_OK


def revoke(session: Session, email: str, out: TextIO) -> int:
    try:
        change = console.set_platform_admin_sync(session, normalize_email(email), grant=False)
    except console.PlatformUserNotFoundError:
        print(f"No account with email {email}", file=sys.stderr)
        return EXIT_UNKNOWN_USER
    except console.LastPlatformAdminError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    if change.changed:
        print(f"Revoked platform admin from {change.user.email}", file=out)
    else:
        print(f"{change.user.email} is not a platform admin", file=out)
    return EXIT_OK


def list_admins(session: Session, out: TextIO) -> int:
    admins = console.list_platform_admins_sync(session)
    if not admins:
        print("No platform admins", file=out)
    for user in admins:
        print(user.email, file=out)
    return EXIT_OK


def reset_link(session: Session, email: str, out: TextIO) -> int:
    """Print a single-use password reset link for the account, to hand over."""
    try:
        issued = password_reset_links.issue_from_shell(session, normalize_email(email))
    except LookupError:
        print(f"No account with email {email}", file=sys.stderr)
        return EXIT_UNKNOWN_USER
    base_url = app_settings_service.get_runtime_config_sync(session).app_base_url.rstrip("/")
    expires = issued.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Password reset link for {issued.user.email} (works once, until {expires}):", file=out)
    print(f"{base_url}{issued.path}", file=out)
    if not base_url:
        print(
            "APP_BASE_URL is not set, so this is only the path: open it on the address "
            "you reach tripl at.",
            file=sys.stderr,
        )
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tripl-admin",
        description="Manage tripl platform admins and recover accounts from the shell.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    grant_cmd = commands.add_parser(
        "grant-platform-admin", help="Make an existing account a platform admin."
    )
    grant_cmd.add_argument("email")
    revoke_cmd = commands.add_parser(
        "revoke-platform-admin", help="Take the platform-admin flag away (never the last one)."
    )
    revoke_cmd.add_argument("email")
    commands.add_parser("list-platform-admins", help="Print every platform admin's email.")
    reset_cmd = commands.add_parser(
        "password-reset-link",
        help="Print a single-use password reset link for an account, to hand over.",
    )
    reset_cmd.add_argument("email")
    return parser


def _default_session() -> Session:
    from tripl.worker.db import SyncSessionLocal

    return SyncSessionLocal()


def run(
    argv: Sequence[str],
    *,
    session_factory: Callable[[], Session] | None = None,
    out: TextIO | None = None,
) -> int:
    """Parse ``argv`` and run the command; the testable entry point.

    ``session_factory`` defaults to the worker's sync engine (``worker.db``).
    """
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else EXIT_REFUSED
    stream = out if out is not None else sys.stdout
    session = (session_factory or _default_session)()
    try:
        if args.command == "grant-platform-admin":
            return grant(session, args.email, stream)
        if args.command == "revoke-platform-admin":
            return revoke(session, args.email, stream)
        if args.command == "password-reset-link":
            return reset_link(session, args.email, stream)
        return list_admins(session, stream)
    finally:
        session.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts] tripl-admin``)."""
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
