---
title: Security & Hardening
sidebar_position: 6
---

# Security & Hardening

This page is for operators and security reviewers running a self-hosted tripl instance. It documents what the application enforces today, the two secrets you must manage, how to rotate each one (and what breaks when you do), and a pre-production hardening checklist.

Everything below is verified against the code. Where the platform relies on you (TLS termination, reverse proxy, network isolation), that is called out explicitly.

:::note
tripl does not terminate TLS itself. It is an HTTP application meant to run behind a reverse proxy or load balancer that handles HTTPS. The security posture below assumes that proxy exists — several settings (HSTS, secure cookies, trusting proxy IP headers) are unsafe without it. See [Self-hosting & Deployment](./deployment.md).
:::

## The two secrets

tripl has two independent application secrets. They protect different things, rotate differently, and have very different blast radii. Do not reuse one value for both.

| Setting | Env var | Algorithm | Protects | Rotation impact |
|---|---|---|---|---|
| Encryption key | `ENCRYPTION_KEY` | Fernet (AES-128-CBC + HMAC-SHA256) | At-rest third-party secrets: warehouse passwords, alert-destination secrets, AI/SMTP secrets stored in instance settings | Existing ciphertext can no longer be decrypted — stored secrets must be re-entered |
| Session signing key | `SECRET_KEY` | HMAC-SHA256 | Integrity of session-token lookups in the DB | All sessions invalidated — every user is logged out and must log in once |

Both default to an empty string, and in a non-debug deploy an empty value (or, for `ENCRYPTION_KEY`, an invalid one) **refuses startup** — see [Production startup checks](#production-startup-checks).

### `ENCRYPTION_KEY` — encryption of at-rest secrets

`backend/src/tripl/crypto.py` centralizes a Fernet-based `encrypt_value` / `decrypt_value` pair. Every service that persists a third-party credential runs the value through it before writing the column:

- **Warehouse/data-source passwords** — `datasource_service.py` stores `password_encrypted`; the value is decrypted when the connection is used.
- **Alert-destination secrets** — `_alerting_destinations.py` encrypts the secret on write; the alert worker decrypts it at send time.
- **Instance-settings secrets** — `app_settings_service.py` encrypts the fields `ai_api_key`, `search_embedding_api_key`, and `smtp_password` when they are set through the admin settings UI.
- **Single sign-on** — an organization's OpenID Connect client secret (`org_sso_configs.client_secret_encrypted`) and each sign-in attempt's PKCE code verifier are encrypted with the operator key. SAML 2.0 has no secret: the IdP certificates it stores are public.

`backend/src/tripl/services/stored_secrets.py` lists every stored secret (each `*_encrypted` column and each secret key inside a JSON settings document); a test fails when a new one appears without being listed there. The Enterprise edition can keep them all under a key in your own key management service instead, and re-encrypt them with a command: see [Key management](../enterprise/kms.md).

Behavior of the Fernet layer:

- With a configured key, values round-trip through Fernet.
- With an **empty** key **and** `DEBUG=true`, values are stored as-is (plaintext) so local dev/test runs work without provisioning a key.
- With an empty key in production, startup is blocked — so the plaintext fall-through can only ever execute in dev/test.
- `decrypt_value` raises `InvalidToken` when a key is set but the ciphertext is corrupt or was written under a different key. Callers surface that as a connection/send error to the operator rather than crashing.

Generate a key with:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

### `SECRET_KEY` — session-token signing

Login issues a random session token (`secrets.token_urlsafe(32)`) and sets it as the session cookie. The **token itself is never stored** — only its HMAC-SHA256 digest, keyed by `SECRET_KEY`, lands in the `user_sessions` table (`auth_utils.hash_session_token`). On each request the cookie value is re-hashed with the same key and looked up.

Keying the hash with `SECRET_KEY` (instead of a bare SHA-256) means a leaked session-hash column is useless without the secret, and tokens cannot be precomputed or rainbow-tabled.

Generate a key with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

:::warning
API keys are **not** keyed by `SECRET_KEY`. They are stored as a plain SHA-256 of the raw token (`api_key_service.py`). Rotating `SECRET_KEY` therefore logs out interactive (cookie) users but leaves issued API keys working. Rotating `ENCRYPTION_KEY` affects neither sessions nor API keys — only encrypted at-rest secrets.
:::

## Rotating the secrets

### Rotating `SECRET_KEY`

1. Generate a new value (command above).
2. Set `SECRET_KEY` in the environment and restart the API and worker.
3. Every existing session-token hash was computed with the old key, so every lookup now misses. **All users are logged out and must log in once.** No data is lost; new logins immediately work.

Stale session rows are harmless — they no longer match any cookie and are cleaned up lazily as they expire (`SESSION_TTL_HOURS`, default 168h / 7 days), or whenever the owning user next logs in.

### Rotating `ENCRYPTION_KEY`

There is **no automated re-encryption tool** and the current implementation uses a single Fernet key (not `MultiFernet`), so there is no rolling/overlap window. Rotation is therefore a deliberate, operator-driven re-entry:

1. Before rotating, make sure you can re-supply every stored secret: each data-source password, each alert-destination secret, and the instance settings `ai_api_key`, `search_embedding_api_key`, and `smtp_password`.
2. Set the new `ENCRYPTION_KEY` and restart the API and worker. (The Fernet instance is cached per process via `lru_cache`, so a restart is required for the new key to take effect.)
3. Every secret encrypted under the old key now fails to decrypt with `InvalidToken`. Re-enter each one through the UI so it is re-encrypted under the new key. Until you do, warehouse connections, alert sends, and the affected AI/SMTP features will error.

:::danger
If you lose `ENCRYPTION_KEY` and have no backup, the encrypted secrets are unrecoverable. They must be re-entered from their original sources. Back up this key separately from the database.
:::

## Production startup checks

`Settings.assert_production_ready()` (in `backend/src/tripl/config.py`) runs from the FastAPI lifespan on startup. When `DEBUG=false` it collects every problem and raises `RuntimeError` (refusing to boot) if any of the following hold. It is a no-op when `DEBUG=true`, and it does **not** run for tests or CLI tools that import `Settings` directly.

| Check | Failure condition |
|---|---|
| `ENCRYPTION_KEY` present | Empty → secrets would be stored as plaintext |
| `ENCRYPTION_KEY` valid | Set but not a valid Fernet key |
| `SECRET_KEY` present | Empty → session hashes would be unkeyed/guessable |
| `SESSION_COOKIE_SECURE` | `false` → cookies would be sent over plain HTTP |
| CORS origins resolved | Empty → no browser can call the API |
| CORS not wildcard | Resolves to `*` → credentialed cookie requests break |
| No dev DB/broker creds | `DATABASE_URL`, `SYNC_DATABASE_URL`, or `RABBITMQ_URL` still contain the dev-default credentials `tripl:tripl` / `guest:guest` |

After these checks, the active secret cipher runs its own check: a no-op for the Fernet key, and for an Enterprise instance under a key management service, a round trip through it ([Key management](../enterprise/kms.md#startup-check)). The worker runs the same check when it starts.

This is a fail-fast guard, not a substitute for the full checklist below. It only inspects configuration values; it cannot verify your TLS, network, or proxy setup.

`DEBUG` is normalized: the strings `release` / `prod` / `production` map to `DEBUG=false`, and `dev` / `development` map to `DEBUG=true`.

## TLS, HSTS, and security headers

`SecurityHeadersMiddleware` (`backend/src/tripl/middleware/security_headers.py`) is added when `SECURITY_HEADERS_ENABLED=true` (the default). It appends the headers below to **every** response, including error responses, and never overrides a header a downstream handler already set.

Always applied:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`

Conditional:

- **Content-Security-Policy** — emitted only if `CONTENT_SECURITY_POLICY` is set, *or* if `SERVE_FRONTEND=true` and no explicit policy is given (then a SPA-tuned default is applied):

  ```
  default-src 'self'; script-src 'self';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data: blob:; font-src 'self' data:;
  connect-src 'self'; frame-src https://www.figma.com https://embed.figma.com;
  frame-ancestors 'none'; base-uri 'self'; form-action 'self'
  ```

  The UI's fonts (Inter and JetBrains Mono) are bundled with the app and served from its own origin, so the policy names no font host. With GCS photo storage, `https://storage.googleapis.com` is also added to `img-src`. If you do not serve the SPA from the API and you do not set `CONTENT_SECURITY_POLICY`, **no CSP header is emitted** — set one at your proxy or via the env var.

- **HSTS** — emitted only when `HSTS_ENABLED=true`, as `Strict-Transport-Security: max-age=<HSTS_MAX_AGE_SECONDS>; includeSubDomains` (default max-age 31536000, one year). HSTS is opt-in by design: enabling it without HTTPS in front would make the site unreachable over HTTP with no way back. Turn it on only once TLS and `SESSION_COOKIE_SECURE=true` are in place.

:::tip
The `'unsafe-inline'` in `style-src` is required by the bundled UI
(Radix/Tailwind/recharts/CodeMirror inline styles). The two Google origins are
limited to the stylesheet and font files used by the bundled page; scripts
remain `'self'` only, including the external pre-React theme initializer. If
you tighten the CSP, test the SPA before shipping it.
:::

The middleware reads `CONTENT_SECURITY_POLICY`, `SERVE_FRONTEND`, `HSTS_ENABLED`, and `HSTS_MAX_AGE_SECONDS` **once at construction time**. Changing any of them requires an API restart to take effect.

## Rate limiting

`backend/src/tripl/middleware/rate_limit.py` provides a token-bucket limiter for unauthenticated auth endpoints. With `REDIS_URL` set the buckets live in Redis, so all workers and replicas share one quota per client; without Redis, or while Redis is unreachable, each worker process enforces its own in-memory bucket:

| Route | Setting | Default |
|---|---|---|
| `POST /api/v1/auth/login` | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 / minute |
| `POST /api/v1/auth/register` | `RATE_LIMIT_REGISTER_PER_HOUR` | 3 / hour |
| `POST /api/v1/auth/password-reset/request` | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 / minute |
| `POST /api/v1/auth/password-reset/confirm` | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 / minute |
| `GET /api/v1/auth/status` | Shared status limiter | 30 / minute |
| `GET /api/v1/auth/invitations/{token}` | Shared status limiter | 30 / minute |
| `POST /api/v1/auth/invitations/{token}/accept` | `RATE_LIMIT_REGISTER_PER_HOUR` | 3 / hour |
| `POST /api/v1/auth/verify-email/request` | Own verification limiter (fixed, not configurable) | 10 / hour |
| `POST /api/v1/auth/verify-email/confirm` | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 / minute |
| `GET /api/v1/auth/google/start` | Own sign-in limiter (fixed, not configurable), shared by start and callback (and, on an Enterprise server, single sign-on) | 20 / minute |
| `GET /api/v1/auth/google/callback` | The same sign-in limiter | 20 / minute |

The verification-link request has a bucket of its own, so resending a link does
not use up the login or sign-up quota, and a signed-in caller cannot turn the
operator's mail relay into a mail cannon.

The two password-reset routes and the verification-link confirm reuse the
**login** limiter (same bucket and setting), so they share its per-IP quota. Buckets are keyed per
`(limiter name, client-IP)`, so routes on **different** limiters (login vs
register) do not share quota, while routes that reuse a limiter (the password-reset
routes on the login limiter) do. Exceeding a limit returns `429 Too Many Requests`
with a `Retry-After` header. To turn rate limiting off entirely, set
`RATE_LIMIT_ENABLED=false`.

Set an individual route's limit to `0` to disable that route limiter while
leaving the other one active. Use `RATE_LIMIT_ENABLED=false` to disable both.

**Client-IP source — read this before exposing the API directly.** By default the limiter keys on the real socket peer (`request.client.host`), which is correct when the API is the edge (including the single-container `SERVE_FRONTEND` deploy). `RATE_LIMIT_TRUST_FORWARDED_FOR` defaults to **false** on purpose: a raw `X-Forwarded-For` is attacker-controlled, so trusting it on a directly-exposed API lets an unauthenticated caller rotate the header per request and bypass the limit entirely. Enable it **only** behind a trusted proxy that *overwrites* `X-Real-IP` with the true client address on every request (the shipped nginx config does this). When enabled the limiter prefers `X-Real-IP`, falling back to the leftmost `X-Forwarded-For` entry.

:::warning
Without Redis the limiter is **per worker, in memory**: with multiple Uvicorn/Gunicorn workers or replicas, each holds its own buckets, so the effective limit is roughly the configured value times the worker count. Set `REDIS_URL` (the shipped compose files do) to make it one aggregate cap.
:::

## Authentication, sessions, and cookies

### Self-service registration

`POST /api/v1/auth/register` is the only unauthenticated way to obtain an
account, and it is governed by a single instance setting, `REGISTRATION_MODE`
(`Settings → Instance → Security & access → Registration`):

| Mode | Behaviour |
|---|---|
| `open` (**default**) | Anyone who can reach the instance can create an account (rate-limited). |
| `disabled` | New signups are refused with `403`. |

:::danger Read this before you expose an instance publicly
**The default is `open`, and `open` means anyone who can reach the URL can
create an account.** A new account joins the default organization as a
**member**. It is a member of **no project**: existing projects stay invisible
to it (`404`) until the creator or an org owner or admin adds it (see
[Roles and access control](#roles-and-access-control-rbac)). A stranger who
registers can still immediately **read**:

- the **member roster** (`GET /api/v1/users`: names, emails, organization roles);
- each workspace-global data source's **name, database type and health
  status**. Connection details (host, port, username, whether a password is set,
  TLS settings) are for org owners and admins only and redacted from everyone
  else, and the password itself is never returned to anybody. Reading the
  warehouse's **table and column names** of a workspace-global source
  (`GET /api/v1/data-sources/{id}/schema`) takes an org owner or admin, or an
  editing role in a project the source is in scope for (one that scans it, or
  any project when nobody scans it), because the scan, metric and fact-table
  forms drive their column pickers off it; a member who only views projects gets
  `403`. For a source owned by a project it takes an editing role in that
  project (a **viewer** member gets `403`). `/stats` is org owner/admin-only.
  Every data-source route is fenced to the request's organization: another
  organization's source answers `404`, as an unknown id does, and a project can
  never point a scan, metric or fact table at it.

…and **write**:

- create **projects of their own** (and demo workspaces), and edit everything in
  them except scan configs, which stay org owner/admin-only.

…and, the part that is easiest to miss:

- **run read-only SQL of their own writing against a workspace-global warehouse
  that no project scans.** A fact table and a `sql`-kind metric are both
  free-text `SELECT` statements, saved by an editor and then executed by the
  worker under an owner-configured warehouse credential. An editor never sees the
  credential, and cannot point one at a warehouse that is identifiably another
  project's. Read "identifiably" literally: a workspace-global data source that
  **no** project scans is nobody's in particular, so it is in scope from every
  project, including the one the stranger just created. Within that scope,
  **whatever that credential can read, they can read**.

Once someone is added to a project, an `editor` member edits that project's plan
and can run the same kind of SQL against the warehouses it uses.

So on an internet-reachable instance with `open` registration, a stranger who
registers can become a read-only SQL user on any warehouse no project claims.
That is the decision `REGISTRATION_MODE` is really making.

Deleting a project and creating or editing a data source remain org
owner/admin-only.

If your instance is reachable from the internet, decide the policy **before**
the first deploy, not after. Setting `REGISTRATION_MODE=disabled` (or flipping
Registration to **Disabled** in the UI) closes it.
:::

The default is `open` for historical reasons: it used to be the only way to
onboard anyone. **That is no longer true** — an org owner or admin can now
invite people directly (see below), so a closed instance can still add exactly
the people they name. Closing registration is the right end state for a publicly
reachable instance.

- **First-owner bootstrap is always exempt (self-hosted).** On an instance with
  **no users**, the first registration is accepted regardless of the mode and
  becomes the owner of the default organization **and** the platform admin.
  A fresh (or reset) deploy is therefore always claimable; every *later* signup
  is subject to the policy. With `DEPLOYMENT_MODE=hosted` there is no such
  exception, and a sign-up never grants platform admin; see
  [Email verification](#email-verification).
- **Adding a teammate to a closed instance:** invite them. **Settings → Organization
  → Invitations** takes an email and an organization role and returns a
  single-use link.
  Nothing about the instance-wide policy changes, so there is no window during
  which strangers can sign up. See [Invitations](#invitations) below.
  (The old workaround — flip Registration to **Open**, have them register, flip
  it back — still works, since the override applies immediately rather than at
  process start. But it genuinely opens the instance for the length of that
  window, so prefer an invitation.)
- **No account enumeration.** The policy check runs *before* the duplicate-email
  lookup, so a closed instance returns the same `403` for a registered address
  and an unknown one.
- `GET /api/v1/auth/status` reports `registration_enabled` (instance-wide, no
  per-account information), so the sign-in screen hides the sign-up form
  entirely on a closed instance instead of letting a visitor discover the policy
  from a `403`. It also reports `email_configured` (SMTP host and From: address
  both set), so the forgot-password form says up front that no reset link can
  arrive; the unauthenticated reset request already returned the same flag.
  Finally it reports `deployment_mode` and `email_verification_required`. A
  hosted instance always reports `has_users: true`, so the endpoint does not
  reveal whether it is empty.

Rate limiting (`RATE_LIMIT_REGISTER_PER_HOUR`) still applies on top and is *not*
a substitute: it slows signups, it never closes them.

### Invitations

An org owner or admin can add one named person without touching the
instance-wide policy. **Settings → Organization → Invitations → Invite a member** takes an email and
an organization role (`owner`, `admin` or `member`, default `member`) and returns
a single-use link into the organization the request acts in.

| Property | Behaviour |
|---|---|
| Who can issue one | **Org owner or admin, from an interactive session.** The route uses the dependency that rejects API keys of every scope, so an automation token can never mint an identity. Inviting at `owner` takes an owner (`403 Only an owner can manage owners`). |
| Works while registration is closed | **Yes** — that is the point. Redemption is a separate mechanism from the instance-wide door, not a special case inside it. |
| Address | Fixed by the invitation. The redeem form never asks for one, so a link cannot be turned into an account for someone else. |
| Role | The organization role, fixed by the inviter at invite time (`invitations.org_role`). The invitee cannot influence it. |
| Lifetime | 72 hours, single use. Re-inviting the same address invalidates the previous link. |
| Delivery | The link appears **once**, in the response to creating it, and is never retrievable afterwards. Copy it then. This is deliberate: SMTP is optional, so handing the link over out of band has to be a first-class path. When the operator has SMTP configured, the link is also emailed, always through the operator's mail server (never an organization's). |
| Existing accounts | An address that already has an account can be invited into an organization it is not yet in. Accepting while signed in adds the membership, and only when the signed-in account's email equals the invitation's (case-insensitive); any other account gets `403` and the link stays unused. On a hosted instance the signed-in account must also have [verified](#email-verification) its address (`403` otherwise). |
| Storage | Only a keyed HMAC digest of the token is stored, like session and reset tokens — a leaked `invitations` table is useless without `SECRET_KEY`. |
| Rejection | Unknown, expired and already-used links return one identical error, so a rejected redemption never reveals which it hit. |
| Revoking | **Settings → Organization → Invitations** lists the organization's pending invitations; revoking one kills its link immediately. An invitation into another organization answers `404`. |

Endpoints: `POST`/`GET` `/api/v1/users/invitations`, `DELETE
/api/v1/users/invitations/{id}` (all org owner/admin-only), plus the unauthenticated
`GET /api/v1/auth/invitations/{token}` preview and
`POST /api/v1/auth/invitations/{token}/accept`.

### Email verification

Every account records whether its email address is verified. The check is
**enforced only with `DEPLOYMENT_MODE=hosted`**; a self-hosted instance records
the flag and blocks nothing.

| Property | Behaviour |
|---|---|
| Gate | On a hosted instance an unverified account gets `403 Email address not verified` on every route outside `/api/v1/auth/*`, with a browser session or an API key. It can still sign out, read `/auth/me`, request and confirm a verification link, and preview an invitation. A self-hosted instance never gates. |
| Sign-up | Hosted sign-up needs the operator's SMTP (`503 Email delivery is not configured` otherwise, before anything is created) and sends the link right after the account is made. A failed send is logged, and the user resends with **Resend email** on the **Check your inbox** screen. |
| Link | `{APP_BASE_URL}/verify-email?token=...`, sent through the operator's relay. Single use, 24 hours. Requesting a new one deletes the account's older unused links; confirming one deletes the rest. On a self-hosted instance `POST /auth/verify-email/request` answers `204` and sends nothing. |
| Confirming | `POST /auth/verify-email/confirm` needs the signed-in browser session of the account the link was sent to. Without a session it answers `401 Sign in to confirm your email address.` and the link stays unused; the web page sends the visitor to sign in and back to the link. A session of any other account gets the same `400` as a dead link, and the link stays unused. Holding the link alone therefore proves nothing. |
| Other sessions | Confirming signs out every other session of the account and keeps only the one that confirmed. The owner of the address has just proved control, so a session opened by someone else (for example with a password that was guessed or reused) ends there. |
| Storage | Only a keyed HMAC digest of the token is stored, like session, reset and invitation tokens. |
| Rejection | Unknown, expired and used tokens, and tokens sent to another account than the signed-in one, return one identical `400`. |
| Verified by construction | A self-hosted instance marks **every** account verified when it is created (sign-up, invitation, the first account), and never checks the flag. On a hosted instance an account that completes a password reset is marked verified, since the reset link was mailed to the address. Accounts that existed before verification was introduced were marked verified by the upgrade. |
| Invitations | On a hosted instance, redeeming an invitation into a **new** account does not verify the address: the inviter received the raw link in the API response, so using it proves nothing about who reads that mailbox. The new account is sent a verification link (a failed send is logged) and must confirm it like a sign-up before it can use the app. A signed-in account accepts an invitation only once verified. |
| Platform admin | Granted only when an account whose address is listed in `PLATFORM_ADMIN_EMAILS` confirms the emailed verification link while signed in as itself. Sign-up, invitations and password reset never grant it, so the grant always follows proof that the person controls both the address and the account. |

### Single sign-on and provisioning

Single sign-on per organization (OpenID Connect, SAML 2.0) and provisioning
over SCIM 2.0 are part of the [Enterprise edition](../editions.md). Their
security controls are in
[Single sign-on and provisioning](../enterprise/sso-and-scim.md#security-notes).

### Audit webhook {#audit-webhook}

:::info Enterprise
The audit webhook is part of the [Enterprise edition](../editions.md). See [its security notes](../enterprise/audit.md#security-audit-webhook).
:::

It sends every new audit entry, signed, to an HTTPS endpoint. Its security
controls are documented with that edition.

### Passwords

Passwords are hashed with **scrypt** (`backend/src/tripl/auth_utils.py`): `N=2^16`, `r=8`, `p=1`, 16-byte random salt, verified with a constant-time comparison (`hmac.compare_digest`). The cost was chosen to harden against offline cracking while still running on a constrained ARM SBC. On a successful login, a hash produced with an older (lower) `N` is opportunistically re-hashed to the current parameters.

### Self-service password reset

Two endpoints back the "Forgot your password?" flow (`backend/src/tripl/api/v1/auth.py`, `services/auth_service.py`):

- `POST /api/v1/auth/password-reset/request` `{ email }` — **always** returns `200` with the same neutral message whether or not the address is registered, so it cannot be used to enumerate accounts. A token is minted and emailed **only** when the instance can actually send — `SMTP_HOST` *and* `SMTP_FROM_ADDRESS` both set — *and* an account matches; otherwise nothing is stored or sent. Both, because the send returns early without a `From:` address, so a host alone would mint a token, drop the mail, and still promise a link. The response also carries an instance-wide `email_configured` flag (identical for every caller) so the UI can fall back to "contact your owner" copy — this reveals nothing about any specific account.
- `POST /api/v1/auth/password-reset/confirm` `{ token, new_password }` — redeems the token and sets the new password. `new_password` must satisfy the **same policy as registration** (enforced at the schema boundary; `≥ 12` chars with a digit and a symbol). Invalid, expired, and already-used tokens are all rejected with an identical `400` so a rejected token never reveals which case it hit.

Token handling mirrors session tokens and never trusts the raw value:

- The token is `secrets.token_urlsafe(32)` (~256 bits). The **raw token is never stored** — only its HMAC-SHA256 digest keyed by `SECRET_KEY` (`auth_utils.hash_session_token`) lands in `password_reset_tokens`, so a leaked column is useless without the secret.
- **Single-use and short-lived**: each token carries `expires_at` (1 hour, `auth_service.PASSWORD_RESET_TTL_HOURS`) and `used_at`. Confirming marks it used, drops any other outstanding token for that user, clears all of the user's active sessions (a reset ends other logins) and revokes all of the user's API keys, so a key minted by whoever held the old password stops working too. Keys have to be issued again after a reset.
- **Marks the address verified, grants nothing else**: the link was mailed to the account's address, so a completed reset counts as email verification. It never grants platform admin, even for an address listed in `PLATFORM_ADMIN_EMAILS`; only the [verification link](#email-verification) does.
- Both routes are **rate-limited** via the shared login limiter (see the rate-limiting table above), and email is sent through the existing alert email channel (`worker/tasks/alerts_channels.py`) as a background task — so a slow SMTP round-trip neither blocks the request nor becomes a timing oracle for whether the account exists.

### Session cookies

The session cookie (`backend/src/tripl/api/v1/auth.py`) is set with:

- `HttpOnly` — not readable from JavaScript.
- `SameSite=Lax` — mitigates CSRF on cross-site state-changing requests while allowing top-level navigations.
- `Secure` — controlled by `SESSION_COOKIE_SECURE` (must be `true` in production; enforced by the startup checks).
- `Path=/`, `Max-Age` = `SESSION_TTL_HOURS` × 3600 (default 168h / 7 days).
- Cookie name `SESSION_COOKIE_NAME` (default `tripl_session`).

Sessions are server-side records (`user_sessions`): each carries an expiry, expired sessions are deleted on access, and logout deletes the matching row. On login, that user's already-expired sessions are also pruned.

### Programmatic access (API keys)

For non-browser clients, tripl issues personal API keys (`api_key_service.py`). The raw token has the shape `tk_<scope-letter>_<random>` (e.g. `tk_r_…` / `tk_w_…`); only its SHA-256 hash is stored, so a leaked DB dump cannot replay tokens. Keys carry a scope (`read` / `write`), an optional expiry, and an optional project binding. They are presented as `Authorization: Bearer <token>` and are resolved before cookie auth. See the [Agent API Guide](../integrate/agent-api-guide.md).

Creating and revoking keys requires an interactive user session. A Bearer key cannot mint a successor or revoke another key, even when it has write scope and no project binding. In an organization that [requires single sign-on](../enterprise/sso-and-scim.md#single-sign-on-oidc), a key works only if it was created from a single sign-on session of that organization, and creating one needs such a session (owners may create keys from any session).

## Roles and access control (RBAC)

Access is decided by the **organization role** (`organization_members.role`:
`owner`, `admin`, `member`), the **project role** of a member
(`project_members.role`: `editor`, `viewer`), and the **platform admin** flag
(`users.is_platform_admin`), plus a two-level API-key scope (`ApiKeyScope`:
`read`, `write`). A self-hosted instance has one organization, the default one.
The **first** registered user becomes its `owner` and the platform admin, so the
instance always has someone who can manage roles and operate it; every later
self-registration joins as `member` — and is refused entirely unless
registration is `open` (see [Self-service registration](#self-service-registration)).
There is no instance-wide role: the old one (`users.role`) has been dropped, and
a guard test (`tests/test_legacy_instance_role_removed.py`) fails the build if
it comes back.

| Who | Can do |
| --- | --- |
| **Org owner** | Everything in the organization: project role `owner` in every project of it, data sources, scan authoring, the audit log, deleting projects, danger-zone resets, tracker and branch settings, members, roles (including owners) and invitations. |
| **Org admin** | Everything an owner can, except making, unmaking or inviting an owner (`403 Only an owner can manage owners`). |
| **Member** | Exactly their project rows: `editor` edits that project's plan, `viewer` reads it; no row is `404`. May create projects of their own and write-scoped API keys. |
| **Platform admin** | The operator settings (security, observability, system, server paths, the photo size cap). **Nothing** inside any organization from the flag: without a membership they see no project (`404`) and connection details stay redacted. The one way in is a time-limited, audited [read-only step-in](#platform-console-and-read-only-step-in). Also the [platform console](../administer/admin-guide.md#platform-console): suspending organizations and granting or revoking the flag. Created by the first registration on a self-hosted instance, or with `tripl-admin` on the server. |

A role in one organization gives nothing in another: the project role is always
computed against the project's **own** organization, and every project lookup is
fenced to the organization the request acts in, so an id taken from a resource
(a comment, a photo, a reviewer) cannot reach across organizations either.

An installed extension may add project roles on top (the Enterprise edition's
[group roles in projects](../enterprise/rbac.md)) through one hook in the same
module, so every gate and list sees them. Such a grant is only ever `editor` or
`viewer`, never `owner`; it counts only for a current member of the project's
own organization, through a grant of that same organization; and it never
changes an organization role. An extension may also take single write
permissions away from an editor, never add one. Without an extension there are
no grants, and a test pins that the answers are exactly the ones above.

### Organization isolation

Organizations are isolated by application code and by database constraints;
there is no row-level security in PostgreSQL. What holds the boundary:

- **Names are per organization.** A project slug is unique within its
  organization (`uq_projects_organization_slug`) and a data source name within
  its organization (`uq_data_sources_organization_name`). Two organizations can
  both have a project `web` and a source `warehouse`; each is reached only
  through its own organization.
- **Every lookup is fenced to the request's organization.** A slug resolves only
  in the organization the request acts in (`services/project_lookup.py`; a
  guard test fails the build on any other `Project.slug` comparison). Another
  organization's project, data source, audit entry or invitation answers `404`,
  as an unknown one does.
- **Lists are per organization.** `GET /projects`, `GET /data-sources`, the
  member roster, a project's audit history, API keys and the notification bell
  (`/me/notifications`, its unread count and mark-read) show only the request
  organization's rows. The bell does not span organizations: a user in two
  organizations reads each one's notifications under its own prefix
  (`/api/v1/orgs/{org}/me/notifications`). A project's audit history falls
  back to a deleted project's slug inside the organization only, so a reused slug
  never inherits another organization's history.
- **A project-bound row stays in its project's organization.** Data sources and
  API keys bound to a project carry a composite foreign key
  `(project_id, organization_id)` onto `projects(id, organization_id)`, so the
  database refuses a source or key that names one organization and points at
  another's project. Rows with no project (workspace-wide sources, unbound keys)
  are not affected.
- **No write lands in an organization by default.** `organization_id` on
  projects, data sources, API keys and invitations has no default value. The
  create paths take the request's organization and raise when none is bound, so
  a code path that forgets the organization fails loudly instead of writing into
  the default one.
- **Reserved slugs.** A project cannot take a slug that names a route
  (`demo`, `orgs`, `new`, `settings`, `api`, `p`, `o` and a few more; the list is
  `RESERVED_PROJECT_SLUGS` in `schemas/project.py`): `422`.
- **Demo limits are per organization.** The cap on live demo workspaces per
  creator is counted inside each organization.

Enforcement lives in `backend/src/tripl/api/deps.py`. The route-facing FastAPI
dependencies compose the checks below:

| Dependency | Rule |
|---|---|
| `get_current_user` | Requires a valid Bearer API key or a valid session cookie; else 401. Binds the request's organization (a key's own; a URL naming another answers `404`). |
| `require_write_scope` | A `read`-scope API key is blocked from mutation endpoints (session users have no scope tag and pass) |
| `get_editor_user` | With a project slug: an editing project role (`403` for a viewer member). Without one: membership of the organization (`403 Organization membership required`). |
| `get_org_member_user` | Any member of the organization (the member roster) |
| `get_owner_user` | Org owner or admin of the request's organization **and** an interactive session (API keys of any scope get `403`). With a project slug, also project role `owner` in that project, which only an owner or admin of the project's own organization holds. |
| `get_key_reachable_owner_user` | The same, but a write-scoped API key is admitted. Used by exactly one route, the metrics replay. |
| `get_settings_admin_user` | `/settings`: a platform admin, or an owner/admin of the default organization; interactive session only. |
| `require_platform_admin` | `users.is_platform_admin`, interactive session only (`403 Platform admin session required` for a key). Also applied by `/settings` to any write touching an operator field (`403 Platform admin required`). |

### Organization roles, project access

What a user can reach inside the organization is decided per project
(`project_members`, checked in `services/project_access.py`):

| Who | Sees the project | Edits its plan | Manages its members, renames / resets it |
|---|---|---|---|
| An org `owner` or `admin` of the project's organization | Always, with no membership row | Yes | Yes |
| The project's creator | Yes (added as an `editor` member when the project is created), until removed | Yes, unless they are a `viewer` member | Yes, while they hold an editing role |
| An `editor` member | Yes | Yes | No |
| A `viewer` member | Yes | No | No |
| Anyone else, a platform admin included | **No: `404 Project not found`** (a platform admin during a [step-in](#platform-console-and-read-only-step-in): yes, read-only) | No | No |

Deleting a project is for the organization's owners and admins only. The
creator's management rights need an editing role: a creator demoted to a
`viewer` member gets `403` on member changes, rename and reset; a creator who
was removed from the project gets `404` like any other non-member.

- **A non-member does not see the project at all.** Every `/projects/{slug}/...`
  route, `/activity/projects/{slug}` and `/projects/demo/{slug}/...` answers
  `404` with `"Project not found"`, the same answer an unknown slug gets, so the
  response does not reveal whether the slug exists. The project is also absent
  from `GET /projects`, the workspace activity feed and data-source listings. A
  data source bound to a project (`project_id` set) is hidden from non-members of
  that project, including `GET /data-sources/{id}` and its `/schema`. A
  workspace-global source stays listed, but its scan counts and "used by" links
  cover only the caller's projects.
- **The membership row is authoritative.** Nothing caps it from outside. When
  organizations arrived, the upgrade capped every former instance viewer's rows
  at `viewer` once, so no one gained write access.
- **A row counts only for a member of the project's organization.** Adding a
  member refuses anyone outside it, and leaving the organization deletes the
  rows. A row that outlives both (a project moved to another organization, say)
  grants nothing: its holder gets `404` like any other non-member, receives no
  notifications or alert emails from the project, and is not listed on the
  project's **Access** settings page.
- **The gate runs before anything else.** `require_project_membership` is
  mounted with `get_current_user` on every authenticated router
  (`api/v1/router.py`), so it answers ahead of the route's own gates, parameter
  validation and handler. The resolved role is stashed on
  `request.state.project_role`, and `get_editor_user` reads it to refuse a
  viewer member (`403`). `tests/test_project_membership.py` fails the build if a
  slug route is mounted without the gate.
- **API keys act as their user, in their organization.** A key is bound to the
  organization it was minted in. An unbound key reaches exactly the projects its
  user is a member of there. Minting a key bound to a project needs membership:
  for a non-member the slug answers `404`, as if it did not exist. A
  project-bound key used on any other project's slug also gets `404 Project not
  found`, the same answer as an unknown slug, even when its user is a member of
  that project.
- **One existence signal remains, inside the organization.** Slugs are unique
  per organization, so creating a project with, or renaming one to, a slug that
  is already taken in the same organization answers `409`, whether or not the
  caller can see the project that holds it. Another organization's slugs are
  invisible: the same slug there is free.
- **Removal takes effect at once.** Removing a member also removes their
  event-type ownerships and their pending branch-reviewer assignments in that
  project, and a live-updates stream (`/projects/{slug}/events/stream`) they
  have open is closed within one heartbeat.
- **Collaborators must be members.** Adding an event-type owner or a branch
  reviewer who is not a member of the project answers `422` with
  `"User is not a member of this project"`, and adding a project member who is
  not a member of the project's organization is refused too.
- **New users see nothing until added.** A user who registers or accepts an
  invitation is a member of no project. The creator or an org owner or admin
  adds them in **Settings → Project → Access**, or through
  `POST /api/v1/projects/{slug}/members`.
- **Rejoining starts from the default.** A project role counts only while its
  holder is a member of the project's organization. When someone joins an
  organization (an invitation, an owner adding them, single sign-on or SCIM),
  any project roles they still held in its projects from an earlier membership
  are deleted, so they start on the organization's default project role rather
  than on a role nobody gave them this time.
- **Demos belong to their creator.** A demo workspace starts with its creator as
  its only member. A reset rebuilds the project row, and the members it had
  before the reset who are still in the organization are granted on the new
  row.

Managing members (`POST`, `PATCH /{user_id}` and `DELETE /{user_id}` under
`/projects/{slug}/members`) is limited to the organization's owners and admins
and the project's creator, from a browser session only: an API key cannot grant
access, whatever its scope. Every change is audited as `project.member_add`,
`project.member_update` or `project.member_remove`.

**Upgrading.** The migration that introduced membership kept existing access: every existing non-owner user became a member of every existing **non-demo** project, with their instance role (`editor` or `viewer`), and each existing demo got only its creator. The migration that introduced organization roles made every instance owner an owner of the default organization, everyone else a member, and capped the project rows of former instance viewers at `viewer`. Users created after the upgrade see no project until someone adds them.

Every project response (`GET /projects`, `GET /projects/{slug}`, and the create,
update and demo reset responses) carries `my_role` (`owner`, `editor` or
`viewer`) and `can_mutate`. `can_mutate` combines the caller's project role and
API-key scope, and is the same predicate the routes enforce, so a client can
hide write controls that would only answer `403`. It is a hint for display; the
routes still decide.

Some surfaces carry a stricter gate than the role table alone implies:

| Surface | Gate | Why |
|---|---|---|
| Scan configs — create / update / delete, `preview`, `preview-jobs`, `dry-run`, `dry-run-jobs` | `get_owner_user` (org owner or admin, interactive session) | A scan config is the project's **ingestion contract**: it drives event-type discovery and schema drift, and its `base_query` is recorded in the audit log. Owning that is the organization's owners' and admins' decision. This gate is **not** what admits a warehouse into a project — that is decided by ownership, see the row below — but creating one does narrow who *else* may reach a workspace-global data source: scanning it claims it for this project, and every project that does not scan it is refused from then on. Delete the last scan config on that source and it is shared again. |
| Fact tables and `sql`-kind catalog metrics — create / update / delete, `preview`, `metrics/preview`, `metrics/fact-preview`, `metrics/series-preview` | `get_editor_user` | These are also free-text `SELECT` statements run against an owner-configured credential, and they are **editor**-authored on purpose: maintaining the metrics catalog is what the editor role is for. The consequence is stated plainly rather than hidden — **an editor is a read-only SQL user on every warehouse their projects already use.** Scoping is by **ownership**, one rule for the save, the preview and the worker that later runs the statement (`services/data_source_scope`): a data source is refused when its `project_id` names a different project, or when it is workspace-global (`project_id IS NULL`) and some *other* project scans it while this one does not. Two consequences are worth reading twice — a workspace-global source that **no** project scans is bindable and previewable from **every** project, which is what the NULL means; and a source **owned** by another project stays refused even when this project scans it. The four preview routes write an audit row, because they are the only ones here that leave no stored object behind. |
| `POST /scans/{id}/metrics/replay` | `get_key_reachable_owner_user` | An org owner's or admin's session or write key can replay a stored config over a chosen window. The request and resulting job are recorded in the audit log. |
| `POST /scans/{id}/event-groups/apply` | `get_owner_user` (org owner or admin, interactive session) | Applying saved grouping rules to existing events is org owner/admin-only. Editors do not see the Apply groups action. |
| `POST /scans/{id}/run`, cancelling a job | `get_editor_user` | Running a **stored** config executes no new SQL, so it stays with the role that maintains the plan — and with the API keys that automate it. |
| `PATCH /api/v1/projects/{slug}` (name, slug, retention) | Project **creator**, or an org owner or admin | Identity, not content: an `editor` member edits the plan but does not rename or re-slug the project. |
| `GET /data-sources/{id}/schema` | `get_editor_user` (organization membership), plus an editing role in the owning project for project-owned sources, or org owner/admin or an editing role in a project the source is in scope for when it is workspace-global | Warehouse table and column names. Editors need it for scan, metric and fact-table forms. A project-owned source answers `404` to a non-member of its project and `403` to a viewer member; a workspace-global one answers `403` to a member with no editing role where it may be used. |
| `GET /data-sources/{id}/stats`, and connection details (host, port, username, `password_set`, TLS) on every data-source read | Org owner or admin of the request's organization | Everyone else — a platform admin included — sees a data source's name, type and health, which is all the scan picker and metric card need. |
| `GET /api/v1/projects/{slug}/audit`, `GET /api/v1/projects/{slug}/audit/{entry_id}`, `GET /api/v1/projects/{slug}/audit/actions` | Org owner or admin, interactive session (no API key) | A project's audit history, its **Govern › Audit log** tab. The list carries no payload; a payload is read one entry at a time from the detail route, behind the same gate. A payload re-exposes both of the rows above: `data_source.*` payloads carry the connection details blanked on a direct read, and `scan_config.create` payloads carry `base_query`. The routes read only the project in the path, inside the request's organization: an entry of another project answers `404`, so one organization's admin never reads another's payloads. The slug resolves to a project and the routes match on its id, so a renamed project keeps one trail and a re-used slug inherits nobody's; while no live project answers to a slug, the denormalized label is matched instead, which is what keeps a deleted project's entries readable. Entries that belong to no project (`data_source.*`, `user.*`, workspace `api_key.*`) are read in the organization-wide audit log, which is part of the [Enterprise edition](../editions.md). Passwords were always redacted (`audit_service._redact`). |

That ownership rule is recent, and on an existing instance it changes two stored
configurations. Fact tables used to ask the narrower question "does a
`ScanConfig` bind this data source to this project?" — a question the `sql`-metric
doors could not ask, because a `sql` metric needs no scan at all and creating a
scan config is owner-only, so there was no way for an owner to bless a
warehouse that is queried but never scanned. After the change, a warehouse no
project scans is usable from every project (that is the widening above), and a
fact metric whose fact table points at a source **owned** by another project now
fails collection with a message telling the editor to repoint it, where before a
`ScanConfig` in this project would have let it run. All four doors and the worker
share one predicate, so save, preview and collect cannot drift apart.

`GET /metrics/{id}/generated-sql` is deliberately **not** a stricter surface: anyone
who can read the metric can read its generated SQL, `viewer` included. The
statement is compiled from configuration the metric and fact-table reads already
return to that viewer — the fact table's query, the metric's filters — and
compiling it runs nothing, so it discloses no data and no credential.

Every one of those statements — a scan's `base_query`, a fact table's `sql`, a
metric's `metric_sql` — goes through the same read-only-SELECT gate
(`validate_select_sql_safety`): single statement,
no stacked `;`, no comment markers, no DDL/DML/`UNION` — each of those three
checked **outside** string and quoted-identifier literals, so a value such as
`'Delete Account'` is data rather than a rejected keyword. A keyword or `;` after
a literal that closed is still caught, and an unterminated literal is scanned as
if it were code. So is everything after a literal that holds a backslash:
ClickHouse, BigQuery and Databricks read `\'` as an escaped quote and PostgreSQL
and Trino do not, so where such a literal ends depends on the engine. Calls that leave
the warehouse from inside a `SELECT` — Databricks' `http_request`,
`read_files`, `ai_query` and `remote_query`, the streaming readers, and
`java_method` / `reflect` — are refused as well.

The gate is an accident guard, not the write barrier. It is a keyword blocklist,
so it stops only writes spelled with one of those words — not a write reached
through a function call (`setval`, `lo_create`), a lock clause (`FOR SHARE`) or
session mutation (`set_config`). The barrier is the warehouse credential's own
privileges (and, on PostgreSQL, `default_transaction_read_only=on` pinned on the
connection; Greenplum gets the same pin, Redshift has no such setting, so there the privileges alone;
Trino and Athena have none either).

It also places **no limit on which tables are read**. There is no table
allowlist: a statement that passes is read-only and single, not narrow. Combined
with the row above — editors author fact tables and `sql` metrics — that gives
one rule worth stating on its own:

:::warning The warehouse credential is the real boundary
Give tripl a credential scoped to what tripl should see. A read-only role
limited to the analytics schema is the difference between "an editor can query
our event tables" and "an editor can query our customer table". tripl enforces
*who may author SQL* and *which project's warehouse they may point it at*; it
does not, and cannot, enforce *which tables inside that warehouse* the
credential reaches.
:::

**What is recorded.** Authoring writes an audit row (`fact_table.create`,
`metric_definition.create`, `scan_config.create`, and their update/delete
counterparts), and so do the four preview routes (`fact_table.preview`,
`metric.preview`, `metric.fact_preview`, `metric.series_preview`) — the only
SQL-executing surfaces that leave no stored object behind. Each carries the SQL
that was run, or for `metric.series_preview` the definition it was built from. The audit log
is org owner/admin-only, so they can answer "who ran what, against which data
source, and when" without an editor being able to read those answers.

Additional guards:

- **Project-scoped API keys are fenced** to their own project: a project-bound key may only touch `/api/v1/projects/{slug}/...` routes for its project. Another project's slug answers `404 Project not found`, the same as a slug that does not exist; any instance-wide route without a project `slug` (`/me/...`, `/users`, ...) is rejected with 403.
- **Role changes take effect immediately, without signing anyone out.** `PATCH /api/v1/users/{user_id}` (org owner/admin) takes `{"role": "owner" | "admin" | "member"}` and writes the organization role; the instance-era `editor`/`viewer` are `422` (they both map to `member`, and write rights live on the project row). Roles are read from the database on every request, so the next request already sees the change; sessions are kept. An in-flight request that already passed the auth check completes with the old role.
- **The last owner cannot be demoted** — the API rejects demoting an organization's only remaining `owner` with `400`. The check is serialised per organization by an advisory lock keyed by the organization id, shared with the first-owner decision at registration.
- Role changes are written to the audit log (`audit_service.record`, action `user.role_update`).

:::note
Any member of the organization (a project viewer included) can list its roster (`GET /api/v1/users`), with organization roles; a signed-in account outside the organization gets `403`. Roles gate **mutations and administration**, not visibility of who exists. Treat the roster as visible to every member.
:::

### Docs catalog: note sharing and break-glass reads {#docs-break-glass}

A docs catalog note can be private to its author or shared with specific people
and organization groups ([Sharing](../use/docs-catalog.md#sharing)). The server
checks it on every docs route, and on the search index at query time. A note
the caller cannot read is `404` and is left out of every list, search result,
backlink, export and count. API keys act as their user, so MCP tools and the
CLI follow the same rules. A share never grants project or organization
membership. It is ignored for anyone who is not a member, and a member's
shares are deleted when they are removed from the organization.

Organization owners and admins can read a note that is not shared with them,
but only directly, by its path. This break-glass read exists for audit and
incident response. Each such read is written to the audit log as
`doc.break_glass_read`, naming the note and the reader; opening the note's
sharing settings (who it is shared with) is audited the same way. The answer
is flagged `break_glass: true`, so the app and MCP agents can tell the reader.
The note never appears in their tree, search or counts, and they cannot edit
it unless it is shared with them for editing.

A move cannot widen or narrow a note's readers unless the mover may change
its sharing: a note moved by someone else keeps its previous access. A hidden
note's path is not secret, though: writing, moving or importing onto it is
refused with `409` ("a doc already exists"), which tells a project editor
that something exists there, never what. Changes to sharing are audited as `doc.share_update`,
with the setting before and after and none of the content. Review both actions
in the project's audit log; sending them to a SIEM through the
[audit webhook](#audit-webhook) is part of the [Enterprise edition](../editions.md).

### Platform console and read-only step-in

The platform console (every organization and account on the instance,
suspension, and a platform admin's read-only step-in to an organization) is
part of the [Enterprise edition](../editions.md). Its security controls are
documented with that edition. A suspended organization's members get
`403 This organization is suspended` in every edition: suspension is stored on
the organization, and only the console sets it.

## Outbound requests {#outbound-requests}

tripl connects to hosts its users configure: warehouses, an organization's
identity provider and webhooks. By default a self-hosted instance lets them
point anywhere, internal hosts included, because an operator often runs the
warehouse next to tripl on purpose. (An organization's own AI and
search-embedding endpoints are always held to public addresses, in either
mode.)

With `OUTBOUND_PUBLIC_HOSTS_ONLY=true`, or always with `DEPLOYMENT_MODE=hosted`,
each such host must resolve to public addresses only. The name is resolved
right before every connection, and the connection goes to the address that was
checked, so a name that answers publicly for the check and privately for the
connection (DNS rebinding) is refused too. Redirects are refused (search
embeddings included), and a BigQuery key may only exchange tokens with Google. Turn it on when the people
who configure these hosts must not reach the instance's own network: the
database, the broker or a cloud metadata endpoint.

**Databricks is the exception to "the connection goes to the address that was
checked".** The Databricks SQL connector opens its own HTTPS connection pools
from the hostname and has no way to be told which address to dial while keeping
the name for TLS, so tripl cannot pin it. The workspace host is still resolved
and vetted right before the connection is opened, but the driver then resolves
it again: a name with a short TTL could answer publicly for the check and
privately for the connection. To close that gap, with the setting on a
Databricks host must also be a Databricks workspace hostname — under
`cloud.databricks.com`, `gcp.databricks.com`, `azuredatabricks.net`,
`cloud.databricks.us`, `databricks.azure.us` or `databricks.azure.cn` — whose
DNS answers Databricks controls, not whoever configured the source. This is the
same reasoning that limits a BigQuery key to Google's token endpoint. A
private-link workspace or a proxy in front of one therefore needs the setting
off. The OAuth machine-to-machine token request (service principal
authentication) goes through the same vetted, pinned, redirect-refusing client
as webhooks; cloud fetch, which would download results from cloud storage URLs
the warehouse hands back, is switched off.

**Snowflake follows the same reasoning, by construction.** Its connector also
opens its own connection pools from the hostname. Whatever the host field holds
— an account identifier or a hostname — tripl turns it into a name under
`snowflakecomputing.com` (or `snowflakecomputing.cn`) and refuses anything else,
so the driver only ever reaches a name whose DNS Snowflake controls; with the
setting on, that name is also resolved and refused when it answers privately,
which is what a PrivateLink account does. Unlike Databricks, Snowflake has no
switch to keep large results inline: a result set larger than a few megabytes is
downloaded in chunks from the presigned cloud-storage URLs Snowflake's own
service hands back. Those URLs come from the account, not from anyone who
configured the source, but they are a second set of hosts tripl does not vet.
tripl's scans return aggregates, so they rarely reach that size.

**Trino is pinned like a webhook.** A coordinator can sit on any domain, so
there is no domain rule to lean on; instead the vetted address is pinned. Every
request the Trino client makes — the statement and each result page the
coordinator points it to — goes to the address that was checked, with the
configured name kept for TLS verification and the `Host` header. A result page
on any other host or port is refused, and so are redirects. A password is sent
only over HTTPS.

**Athena is reached by region, never by host.** Its host field takes an AWS
region (or `athena.<region>.amazonaws.com`) and nothing else; the driver
(`boto3`) is pointed at that region's own Athena endpoint, so no setting can
send the signed requests, or the access key's signature, to another host. With
the setting on, the endpoint is also resolved and refused when it answers
privately, which is what an interface VPC endpoint with private DNS does.
Results are read through the Athena API; Athena itself writes each result to
the workgroup's S3 result location, so the key needs write access to that
bucket and nowhere else.

**Values in Trino and Athena SQL are literals, not bound parameters.** The
Trino client renders a bound parameter into an `EXECUTE … USING` literal on
the client side anyway, and Athena's execution parameters take literal text,
so tripl writes the literal itself: every value is a single-quoted string with
each `'` doubled (Trino's string literals have no backslash escapes), every
identifier is double-quoted with each `"` doubled, a JSON path is built only
from identifier-safe segments, and a timestamp is formatted from a parsed
`datetime`. Nothing user-supplied reaches the statement any other way.

## CORS

The effective allow-list is resolved by `Settings.cors_origins()`:

1. `CORS_ALLOW_ORIGINS` (comma-separated explicit origins), else
2. in `DEBUG` mode, the wildcard `*`, else
3. derived from `APP_BASE_URL` if set, else deny all.

Credentialed (cookie) requests require an explicit origin — browsers reject cookies against `*`, and the production startup checks reject both an empty list and a wildcard. The app sends `allow_credentials=true` unless the resolved origin list is exactly `["*"]`. Allowed methods are `GET, POST, PATCH, PUT, DELETE, OPTIONS`; allowed headers are `Authorization`, `Content-Type`, and the request-ID header (`X-Request-ID` by default, configurable via `REQUEST_ID_HEADER`).

## Error and probe hygiene

- Unhandled exceptions return a generic `500 {"detail": "Internal server error", "request_id": ...}` — internal details are logged server-side with the request ID, never returned to the client (`main.py`).
- The unauthenticated `/health` probe returns a generic body (`{"status": "error", "component": "database"}`, HTTP 503) on failure; the underlying DB error (which can leak the DSN, driver, and host) is logged server-side only.
- `/metrics` is only mounted when `PROMETHEUS_METRICS_ENABLED=true`. Keep it on an internal-only ingress path; it is not authenticated by the app.

## Pre-production hardening checklist

Configuration (all enforced by the startup checks unless noted — see [Configuration Reference](./configuration.md) for the full variable list):

- [ ] `DEBUG=false` (or `release`/`prod`/`production`).
- [ ] `SECRET_KEY` set to a long random value, stored in your secret manager.
- [ ] `ENCRYPTION_KEY` set to a valid Fernet key, **backed up separately** from the database.
- [ ] `SESSION_COOKIE_SECURE=true`.
- [ ] `CORS_ALLOW_ORIGINS` (or `APP_BASE_URL`) set to your exact frontend origin — never `*`.
- [ ] `DATABASE_URL`, `SYNC_DATABASE_URL`, `RABBITMQ_URL` use real credentials, not `tripl:tripl` / `guest:guest`.

Transport and headers (your responsibility — not checked by the app):

- [ ] TLS terminated by a reverse proxy / LB in front of the API.
- [ ] `HSTS_ENABLED=true` only after HTTPS is verified end to end.
- [ ] `SECURITY_HEADERS_ENABLED=true` (default); set a reviewed `CONTENT_SECURITY_POLICY` if you are not relying on the SPA default.
- [ ] If a proxy sits in front, set `RATE_LIMIT_TRUST_FORWARDED_FOR=true` **and** confirm the proxy overwrites `X-Real-IP` on every request; otherwise leave it `false`.

Operations:

- [ ] Rate limiting left enabled (`RATE_LIMIT_ENABLED=true`); add a proxy-tier limit if you run multiple workers/replicas.
- [ ] `/metrics` (if enabled) and any admin surfaces restricted to an internal network.
- [ ] First-run account created promptly so self-registration cannot grab the default organization's `owner` and the platform admin.
- [ ] **`REGISTRATION_MODE` decided deliberately. The default is `open`** — anyone who can reach the instance can create an account, read the member roster, and create projects of their own (existing projects stay hidden until someone adds them). Set `REGISTRATION_MODE=disabled` (or Registration → **Disabled** in **Settings → Instance → Security & access**) once your team has accounts. Closing it is not a dead end: an org owner or admin adds people from **Settings → Organization → Invitations**, so you never need to reopen self-registration to onboard someone.
- [ ] Database and broker on a private network; `ENCRYPTION_KEY` and `SECRET_KEY` not committed to the repo or image.

For symptom-level help (login loops, blocked CORS, 429s), see [Troubleshooting & FAQ](../use/troubleshooting.md).
