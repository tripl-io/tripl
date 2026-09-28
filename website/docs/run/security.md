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
| `GET /api/v1/auth/sso/discover` | Shared status limiter | 30 / minute |
| `GET /api/v1/auth/sso/{org}/start` | Own SSO limiter (fixed, not configurable), shared by start, callback and ACS | 20 / minute |
| `GET /api/v1/auth/sso/{org}/callback` | Own SSO limiter (fixed, not configurable), shared by start, callback and ACS | 20 / minute |
| `POST /api/v1/auth/sso/{org}/saml/acs` | Own SSO limiter (fixed, not configurable), shared by start, callback and ACS | 20 / minute |
| `GET /api/v1/auth/sso/link` | Shared status limiter | 30 / minute |
| `POST /api/v1/auth/sso/link` | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 / minute |
| `/scim/v2/{org}/*` | Own SCIM limiter, keyed per token (not per address) | 600 / minute |
| `/scim/v2/{org}/*`, failed authentication | Own limiter, keyed per client address; only `401` answers draw on it, then `429` | 30 / minute |

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

### Single sign-on (OIDC)

An organization owner can connect the organization to an OpenID Connect
identity provider (see [Single sign-on](../administer/admin-guide.md#single-sign-on)).
The controls that matter for security:

| Property | Behaviour |
|---|---|
| Who configures it | Organization **owners** only, from a browser session. Admins, members and API keys get `403`. Every change is audited (`org.sso.*`) without the secret. |
| Outbound requests | Discovery, key (JWKS) and token requests go only to `https` URLs, follow no redirects, time out after 10 seconds and cap the response size. On a hosted instance a host that resolves to a private, loopback or link-local address is refused, and checked again when the request is made, as for organization mail and AI endpoints; the request then connects to the very address that was checked (TLS and `Host` keep the hostname), so a name that re-resolves between the check and the connection (DNS rebinding) cannot reach an internal address. The owner's connection test answers a fixed text per error code, and it and domain verification are rate-limited. The discovery document's `issuer` must equal the configured issuer, and the token and key endpoints are taken from it. |
| Domains | Only addresses at a domain the organization proved with a DNS TXT record (`_tripl-verification.<domain>` = `tripl-verification=<token>`) are accepted. A domain can be verified by one organization per instance. |
| Login state | Each attempt stores a keyed HMAC digest of its `state` (like session tokens), a `nonce` and an encrypted PKCE (S256) verifier. The state is single use, bound to the organization and expires after 10 minutes. The return address (`next`) must be a relative path on the same origin. |
| ID token | Verified with the provider's published keys: `RS256` or `ES256` only (the algorithm comes from the key, `none` is refused), issuer, audience = client ID, `azp` = client ID whenever present (and required with several audiences), expiry, issued-at with 60 seconds of leeway, and the nonce. The token must carry `email` with `email_verified: true`, at one of the organization's verified domains. |
| Errors | A failed sign-in returns to `/auth?sso_error=<code>` with a fixed code. The provider's error text is never echoed. |
| New accounts | Created with a verified address, as organization **members**, never as platform admins. Their password is a scrypt hash of a random secret, so password sign-in takes the same time for them as for any account. |
| Existing accounts | Never linked or signed in automatically. The browser gets a single-use link request (10 minutes), and confirming it on `/sso/link` needs a **session of that account** (`401` otherwise): the provider's sign-in alone proves nothing about the account, since whoever runs a verified domain's provider can name any of its addresses. Only then is the identity linked, and the proving session is replaced by the single sign-on one. |
| Unverified accounts | An account whose address was never verified (a hosted sign-up) is not anyone's yet, so the provider's verified address takes it over clean: the password becomes unusable and every session, API key and pending reset or verification token of the account is dropped before linking. Platform admins are never treated this way. |
| Sessions | A session records how it signed in (`password` or `sso`) and, for single sign-on, which organization. |
| Requiring SSO | With **Require single sign-on**, a session that did not sign in through the organization's provider gets `403 This organization requires single sign-on` inside it. Organization owners' browser sessions are exempt (break-glass), as is a platform admin's read-only step-in. Turning it on revokes **every** organization API key that was not created from a single sign-on session of the organization, owners' included, and such keys are refused with `403`; new keys need such a session, for owners too. |
| Removing a member | Also deletes their single sign-on identities for the organization, and signing in through the provider again does not re-add them until they accept a new invitation. |
| Rate limits | Start and callback share their own bucket (20 a minute per address), apart from password sign-in; an empty bucket redirects to `/auth?sso_error=rate_limited`. Confirming a link is on the login bucket. |

### Single sign-on (SAML 2.0)

An organization can use SAML 2.0 instead of OpenID Connect (see
[Set up SAML 2.0](../administer/admin-guide.md#saml)). Configuration, domains,
account resolution (new, existing and unverified accounts), sessions, requiring
SSO, removing a member and rate limits are exactly as in the table above; what
differs is how the IdP's answer is verified. XML signatures are checked with
`signxml` over `lxml` (no `xmlsec1`), and every check fails closed with a
generic `sso_error` code.

| Property | Behaviour |
|---|---|
| Trust | The IdP's signing certificates are configured by the owner (pasted, or read from pasted metadata; tripl never fetches metadata, so there is no outbound request). Several can be configured for key rotation. They are public and stored as they are; there is no SP key, and tripl's authentication requests are unsigned. A new certificate is checked (it parses, has not expired) when it is saved or when SAML is switched on; a stored one that has expired since does not block other changes, such as turning SSO off. |
| Trust anchor change | Switching the protocol, changing the IdP entity ID, or saving a certificate set that keeps none of the saved certificates deletes the organization's linked identities and pending link tickets of the old provider (the count is in the `org.sso.update` audit entry). Linked members then confirm the link again from their own session, so an owner who points SAML at a key they hold cannot sign in as an already-linked member. |
| Request binding | Each sign-in stores a random request ID on the single-use, 10-minute login state (the state's digest, as for OpenID Connect). The state goes to the IdP as `RelayState` and is bound to the browser by a cookie scoped to `/api/v1/auth/sso/`, `HttpOnly`, `Secure`, `SameSite=None` (the IdP's POST back is cross-site, so a `Lax` cookie would not be sent). A browser keeps a `Secure` cookie only over https (or on `localhost`), so SAML sign-in needs tripl served over https. The response's `InResponseTo` must equal the stored request ID; IdP-initiated (unsolicited) responses are refused (`saml_unsolicited`). |
| Parsing | The posted response is capped in size before decoding, and parsed with entity resolution, DTD loading and network access off and huge trees refused. Any `DOCTYPE` is refused, which rules out entity expansion (billion laughs) and external entities (XXE). |
| Signature | The **assertion** itself must carry a valid enveloped signature by one of the configured certificates; a signature over the response alone is not enough. RSA and ECDSA with SHA-256 or stronger only; SHA-1 is refused. Every later check reads the element the signature verification returned, never the posted document, so a signed assertion moved elsewhere and an unsigned one put in its place (XML signature wrapping) are refused. Exactly one assertion is accepted; encrypted assertions are not supported and are refused (`encrypted_assertion_unsupported`). |
| Assertion checks | Issuer = the configured IdP entity ID; the response's `Destination`, when present, and the bearer subject confirmation's `Recipient` = tripl's ACS URL; `InResponseTo` on both = the stored request ID; `NotBefore` / `NotOnOrAfter` of the conditions and the subject confirmation, with 120 seconds of clock skew; the audience restriction names tripl's entity ID; the status is `Success`. |
| Replay | Each accepted assertion ID is stored per organization until it expires; the same assertion a second time is refused (`saml_replay`), and the login state is single use as well. |
| Email and identity | The email comes from the configured attribute, or from the NameID only when its format is `emailAddress` (a persistent or unspecified NameID is never taken as an email: `email_missing`). It must be at one of the organization's verified domains. The identity is the pair (`saml:` + IdP entity ID, NameID); the prefix means an identity linked over OpenID Connect is never matched by SAML, even with an entity ID equal to the OIDC issuer, and the reverse. The entity ID is at most 507 characters. |

### Provisioning (SCIM 2.0)

An organization owner can let the organization's identity provider create,
update and deactivate its members and groups over SCIM (see
[Provisioning](../administer/admin-guide.md#scim)). The controls that matter
for security:

| Property | Behaviour |
|---|---|
| Endpoint | `/scim/v2/{org}`, outside `/api/v1`. It accepts only a SCIM bearer token of that organization: browser sessions (and so CSRF-able requests) and API keys are refused, and a token of another organization gets `404`. A suspended or deleting organization answers `403`/`404` in SCIM's error format. |
| Tokens | Created and revoked by organization **owners** only, from a browser session (`/api/v1/orgs/{org}/scim/tokens`). A token starts with `tripl_scim_`, is shown once, and is stored only as a keyed HMAC digest, like session tokens; a prefix is kept for display. Revoked tokens are refused at once. A token works only while its creator is an owner of the organization: removing, deactivating or demoting that owner, or their transferring ownership, revokes their tokens (audited with `reason: "creator_no_longer_owner"`), and a token whose creator is no longer an owner is refused in any case. Creation and revocation are audited (`org.scim.token_create`, `org.scim.token_revoke`). |
| New accounts | Created only for an address at one of the organization's **verified** single sign-on domains; any other address is refused (`400 invalidValue`). The address is marked verified, the password is a scrypt hash of a random secret (unusable, so sign-in is through single sign-on), and the account is never a platform admin. |
| Existing accounts | Linked only when the address is at a verified domain of the organization, or the account is already a member; any other existing account gets the same `400 invalidValue` as an unknown address, so a token is no oracle for which addresses have accounts and cannot pull a stranger into an organization. A linked account's password and name are not changed on linking, **except** that an account at a verified domain whose address nobody ever confirmed (an unverified hosted sign-up) is taken over as by a verified SSO sign-in: its password is replaced with an unusable one, its sessions and pending reset/verification links are deleted, **all its API keys are revoked**, and its address is marked verified. Later `displayName`/`name.*` updates change the account's name only for an address at a verified domain. `userName` never changes (`400 mutability`). |
| Visibility | The IdP sees only accounts that are members of the organization or that it provisioned and later deactivated; another organization's users are never listed and answer `404` by id. |
| Deactivation | `active: false` or `DELETE` removes the organization membership with everything [removing a member](../administer/admin-guide.md#members-and-roles) takes away (organization API keys revoked, project access, group memberships and single sign-on identities dropped). The account is kept. The last owner cannot be deactivated (`409`). A member an owner or admin removed by hand cannot be re-activated or re-created by the IdP (`409 mutability`) until they accept a new invitation; a `PUT` without `active` never re-activates. |
| Roles | SCIM never grants owner. The optional admin group mapping promotes the group's members to admin and demotes people removed from it to member; owners are never changed by it. |
| Managed groups | A group the IdP created, or any group the IdP has written to (it sees every group of the organization and matches by name, so a hand-made group with the same name is adopted on its first SCIM write), is managed by SCIM: renaming it, deleting it or changing its members through the groups API gets `409`; only its description stays editable. |
| Audit | Every SCIM write is recorded in the organization's audit log with no acting user and `via: "scim"` plus the token prefix. |
| Rate limits | Each token has its own bucket (600 requests a minute). Failed authentications (`401`) draw on a bucket per client address (30 a minute), then answer `429`. |
| Input limits | `startIndex` is capped at 10⁹; a body nested more than 32 levels deep, or too deeply to parse, is `400 invalidSyntax`; unknown endpoints, unsupported methods and oversized bodies answer in SCIM's error format. |
### Audit webhook {#audit-webhook}

An organization owner can have every new audit entry POSTed to an HTTPS
endpoint (see [Audit webhook](../administer/admin-guide.md#audit-webhook)).
The controls that matter for security:

| Property | Behaviour |
|---|---|
| Who configures it | Organization **owners** only, from a browser session. Admins, members and API keys get `403`. Every change is audited (`org.audit_webhook.*`) without the secret. |
| Signing secret | Generated by the server when the webhook is created or the secret rotated, shown once in that response, and stored encrypted with `ENCRYPTION_KEY`. Reads only say whether one is configured. |
| Signature | `X-Tripl-Signature: sha256=<hex>` is the HMAC-SHA256 of `t=<X-Tripl-Timestamp>.<raw body>` (the literal `t=` included). Receivers should compare in constant time, reject stale timestamps against replay, and drop duplicates by `X-Tripl-Event-Id` (delivery is at least once). |
| Outbound requests | `https` URLs only, no redirects followed (a redirect is a failed delivery), a 10-second timeout per network step and a 15-second deadline for the whole request, enforced even against a receiver that answers a byte at a time; the response body is never read. The owner's test and saves are rate-limited (10 a minute). On a hosted instance a host that resolves to a private, loopback or link-local address is refused when the URL is saved and again at every delivery, and the request connects to the very address that was checked, as for single sign-on, so DNS rebinding cannot reach an internal address. |
| Queue | An entry is queued in the same database transaction that records it, so a committed entry is never lost to a crash and a rolled-back one is never sent. Deliveries of a suspended organization are held. A delivery run leases the rows it claims for longer than a run may last, and writes an outcome only while the row still carries its lease, so two runs never overwrite each other's results. |
| Retention | Delivered queue rows are deleted after 7 days, dead ones after 30 days. The audit log itself is untouched. |

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

Creating and revoking keys requires an interactive user session. A Bearer key cannot mint a successor or revoke another key, even when it has write scope and no project binding. In an organization that [requires single sign-on](#single-sign-on-oidc), a key works only if it was created from a single sign-on session of that organization, and creating one needs such a session (owners may create keys from any session).

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
  member roster, the audit feed, API keys and the notification bell
  (`/me/notifications`, its unread count and mark-read) show only the request
  organization's rows. The bell does not span organizations: a user in two
  organizations reads each one's notifications under its own prefix
  (`/api/v1/orgs/{org}/me/notifications`). The audit log's fallback for a
  deleted project's slug stays inside the organization too, so a reused slug
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
- **Demos belong to their creator.** A demo workspace starts with its creator as
  its only member. A reset rebuilds the project row, and the members it had
  before the reset are granted on the new row.

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
| `GET /api/v1/audit`, `GET /api/v1/audit/{entry_id}`, `GET /api/v1/audit/actions` | Org owner or admin | The list carries no payload; a payload is read one entry at a time from the detail route, behind the same gate. A payload re-exposes both of the rows above: `data_source.*` payloads carry the connection details blanked on a direct read, and `scan_config.create` payloads carry `base_query`. It is scoped to the request's organization: the list and the detail read only that organization's entries, so one organization's admin never reads another's payloads. Within it, `project_slug` is a filter, not a scope, and **Settings → Instance → Audit log** is the org owner/admin screen that reads it that way: the actions belonging to no project (`data_source.*`, `user.*`, workspace `api_key.*`) answer nowhere else. That filter resolves the slug to a project and matches on its id, so a renamed project keeps one trail and a re-used slug inherits nobody's; while no live project answers to a slug, the denormalized label is matched instead, which is what keeps a deleted project's entries readable. Passwords were always redacted (`audit_service._redact`). |
| `GET /api/v1/orgs/{org}/audit/export` | Org owner or admin, interactive session (no API key) | The same rows the feed reads, with their payloads, as one file: the organization's entries and its projects', never another organization's or the platform's own. At most 366 days per request, streamed in pages of 1,000 rows, each page read in its own short transaction so a slow download holds no database connection; rate-limited to a few exports a minute. CSV cells starting with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading apostrophe against spreadsheet formula injection. Each export is audited as `org.audit_export`. See [Exporting the audit log](../administer/admin-guide.md#audit-export). |
| `/api/v1/orgs/{org}/audit/webhook` and its `rotate-secret`, `test` and `deliveries` routes | Organization **owner**, interactive session | Where the whole audit trail is sent, so an owner's alone, like single sign-on. See [Audit webhook](#audit-webhook). |

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
if it were code.

The gate is an accident guard, not the write barrier. It is a keyword blocklist,
so it stops only writes spelled with one of those words — not a write reached
through a function call (`setval`, `lo_create`), a lock clause (`FOR SHARE`) or
session mutation (`set_config`). The barrier is the warehouse credential's own
privileges (and, on PostgreSQL, `default_transaction_read_only=on` pinned on the
connection).

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

### Platform console and read-only step-in

The [platform console](../administer/admin-guide.md#platform-console)
(`/api/v1/platform/...`) is the operator's view of every organization on the
instance. It is gated like the platform settings: `require_platform_admin`, a
browser session only, never an API key. It returns metadata and counts —
names, slugs, statuses, member and project counts, owners' and members'
emails, project names — and no project content.

**Suspension.** A suspended organization answers
`403 This organization is suspended` to every request that acts in it, from a
session or an API key, whichever path form names it; organization resolution
refuses it before any route runs, and an open live-update stream in one of its
projects fails its next membership re-check and closes. It stays in its
members' `GET /orgs` with its status, so the app can say why. A request that
names no organization binds the caller's only **active** one; the suspended
`403` answers only when the caller has no active membership but a suspended
one. Deleting a suspended organization is refused with the same `403` (the
deletion request only moves an `active` organization). Scheduled worker jobs
skip the projects of any organization that is not `active` (suspended, or being
deleted). The default organization can never be suspended (`409`), in either
`DEPLOYMENT_MODE`. The one reader a suspension does not stop is a platform
admin's read-only step-in (below), so an operator can investigate a suspended
organization. Suspending and unsuspending are audited in the organization.

**Step-in** is the only way the platform-admin flag reaches inside an
organization, and it is built to be narrow and visible:

- **Read-only by construction.** While a step-in is active the admin resolves
  in that organization as a `member` with the project role `viewer` on every
  project, so every role check that needs more (editor, owner or admin) fails
  as it would for a viewer. On top of that, any request in the organization
  other than `GET`, `HEAD` and `OPTIONS` is refused with
  `403 Step-in is read-only`, except the two read-shaped `POST` queries
  (`/projects/{slug}/anomalies/signals/query` and
  `/projects/{slug}/events/window-metrics`). There is no write mode.
- **Justified and bounded.** A reason (1 to 500 characters) is mandatory, and
  the step-in expires after its TTL (5 to 240 minutes, 60 by default); it can
  be ended early. Each one is a row in `platform_step_ins`
  (`user_id`, `organization_id`, `reason`, `created_at`, `expires_at`,
  `ended_at`), and only an unexpired, un-ended row grants anything. A second
  step-in by the same admin to the same organization supersedes the first,
  which ends. An active or a suspended organization can be stepped into; one
  being deleted cannot.
- **Session only.** API keys never carry a step-in, so a platform admin's key
  cannot read an organization it could not read before.
- **Audited where the owners look.** Start (`platform.step_in`, with the
  reason, TTL and expiry) and end (`platform.step_in_end`) are written to
  the **target organization's** audit log with the platform admin as the actor,
  so the organization's owners and admins see who stepped in, when and why.
  Natural expiry is recorded as `platform.step_in_end` with `expired: true`,
  written lazily the first time organization resolution or a step-in listing
  sees the expired row (a compare-and-set on `ended_at IS NULL` sets `ended_at`
  and writes the row exactly once). All six console actions (`org.suspend`,
  `org.unsuspend`, `platform.step_in`, `platform.step_in_end`,
  `platform.admin_grant`, `platform.admin_revoke`) sit under **Platform** in
  the Audit tab's action filter.
- **Visible to the admin.** `GET /auth/me` returns `active_step_ins`, and the
  app shows a banner with the end time and an **End now** button on every page
  of that organization.

Owner- and admin-only reads (the audit log itself, data-source connection
details, scan SQL) stay closed during a step-in, since the effective role is
`member`.

Granting and revoking the platform-admin flag (console or `tripl-admin`)
cannot remove the last platform admin, and the console refuses revoking your
own flag. Console grants and revocations are audited as
`platform.admin_grant` / `platform.admin_revoke` with no organization.
`tripl-admin grant-platform-admin` also marks the account's address verified
when it was not (the operator controls the instance), noted in the audit
payload as `marked_verified: true`.

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
