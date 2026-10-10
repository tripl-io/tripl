/**
 * The two ways to a password reset link when the instance cannot email one.
 * An owner or admin of the person's organization creates it on Members
 * (`POST /orgs/{org}/members/{id}/password-reset-link`); when nobody who could
 * do that can sign in, whoever runs the server prints one with
 * `tripl-admin password-reset-link <email>` (backend `admin_cli.py`). The
 * sign-in page and Password & sessions both name them, in these words.
 */
export const RESET_LINK_PLACE = 'Settings › Organization › Members'

export const RESET_LINK_COMMAND = 'tripl-admin password-reset-link'
