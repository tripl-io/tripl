---
title: Administration & Instance Settings
sidebar_position: 1
---

# Administration & Instance Settings

This page is for the people who run a tripl instance: managing members, granting
the right level of access, issuing API keys for agents and scripts, and tuning
the server-wide settings the app exposes in its UI.

tripl is a single multi-user instance ("workspace") that contains every project.
Almost everything an administrator does lives under **Settings**, which has two
contexts:

- **Workspace** — Members, Data sources, API keys, your own Profile and
  Security, and the owner-only **Instance** sections.
- **Project** — per-project configuration (General, Plan rules), covered in the
  user guide rather than here.

:::note Settings vs. environment variables
The **Instance** sections in the UI let an owner override a subset of the
server's configuration, stored in the instance database. Everything else —
database/broker URLs, the encryption key, the application secret — is set only
through environment variables. Sections differ in **when** an override applies —
some at use-time, some on the next restart (see [When changes take effect](#when-changes-take-effect)).
For the full env-var reference (including the variables behind the settings
below) see [Configuration](../run/configuration.md).
:::

## Roles & permissions

tripl has exactly three roles, defined server-side as the `UserRole` enum
(`owner`, `editor`, `viewer`):

| Role | Can read | Can edit plan content | Manage members & roles | Instance settings |
| --- | --- | --- | --- | --- |
| **Viewer** | Projects they are a member of | No | No | No |
| **Editor** | Projects they are a member of | In projects where they are an `editor` member | No | No |
| **Owner** | Every project | Every project | Yes | Yes |

The instance role is only half of the answer: which **projects** a user can see
is decided by [project access](#project-access).

How roles are assigned:

- **The first user to register becomes `owner`.** This guarantees every instance
  has at least one operator who can manage roles. Every subsequent registration
  defaults to **`editor`**.
- A **`viewer`** is never created automatically — an owner has to downgrade a
  member to viewer in **Settings → Members**.
- Only an **owner** can change anyone's role. The API refuses to **demote the
  last remaining owner** (`400 Cannot demote the last remaining owner`), so you
  cannot lock yourself out of the instance.

:::note Claiming a brand-new instance
[`tripl install`](../run/cli.md#tripl-install) provisions and starts a stack, but
it stops at a **running, empty instance** — it does not create this first
account, and no tool can. Registering over the API would mean putting a password
on a command line, and the session cookie the reply sets is `Secure`, so it would
be discarded on a plain-HTTP first run. So the last thing `install` prints is the
instruction to open the URL in a browser and sign up; that first sign-up is the
owner, and everything on this page assumes it has already happened. The
provisioning side is
[Self-hosting & Deployment](../run/deployment.md#install-with-the-cli).
:::

:::warning Role changes sign the user out
When an owner changes a member's role, every active session for that user is
deleted in the same transaction, so the new permissions take effect on their
next request. An in-flight request that already passed authentication still
finishes with the old role; only the next request is affected.
:::

### How permissions are enforced

The backend gates endpoints with role/scope dependencies, not just UI hiding:

- **Editor-or-above** is required for any mutation. Viewers receive `403 Editor
  role required`.
- **Owner** is required for member role changes and all Instance settings.
  Non-owners receive `403 Owner role required`.
- **Project membership** is checked before anything else on every project
  route: a non-member receives `404 Project not found`, and a viewer member who
  tries to change something receives `403`.
- Owner-only endpoints additionally require an **interactive owner session** —
  an API key does not reach them, even a write-scoped key owned by an owner
  (`403 Owner session required`). One route is deliberately exempt: the
  [metrics replay](../integrate/agent-api-guide.md#replaying-metrics), which a
  write-scoped key backed by an owner may call.

## Project access

Each project has its own member list. A user who is not a member of a project
does not see it at all: it is missing from the project list, the activity feed
and data-source listings, and every page and API route under it answers
`404 Project not found`. Instance owners see every project and need no
membership.

| Project role | What it allows |
| --- | --- |
| **Editor** | Read the project and edit its tracking plan, catalog and alerting. |
| **Viewer** | Read the project. |

The instance role caps the project role: an instance **viewer** is a viewer in
every project, even with an `editor` membership.

**Settings → Project → Access** lists the project's members. The instance owner
and the person who created the project can, as long as the creator still holds
an editing role:

- **add a member**: pick someone from the workspace and a role;
- **change a member's role** between Editor and Viewer;
- **remove a member**, who then no longer sees the project. Removal also drops
  their event-type ownerships and pending branch-reviewer assignments in that
  project, and closes any live-updates stream they have open within one
  heartbeat.

The creator's rights last only while they can edit: a creator whose instance
role is `viewer`, or who was switched to a `viewer` member, gets `403` on member
changes, rename and reset, and a creator who was removed from the project gets
`404` like any other non-member. Deleting a project is for instance owners only.

Everyone else sees the list read-only. Each change is recorded in the audit log
(`project.member_add`, `project.member_update`, `project.member_remove`).
Changing access needs a signed-in browser session: an API key cannot add or
remove members.

A few things follow from this:

- **Whoever creates a project is an editor member of it**, and can manage its
  access. The same goes for a demo workspace, which starts with its creator as
  the only member; resetting a demo keeps its members.
- **New accounts see no projects.** Someone who registers or accepts an
  invitation has to be added to each project they need.
- **Event-type owners and branch reviewers must be members** of the project
  (`422 User is not a member of this project` otherwise).
- **API keys act as their user.** A key reaches the projects its user is a member
  of, and a key bound to one project can only be created by a member of it. A
  project-bound key used on another project gets `404 Project not found`, the
  same as an unknown slug.
- **Taken slugs still answer `409`.** Creating a project with, or renaming one
  to, a slug that already exists is refused with `409` even if the caller cannot
  see that project. Slugs are unique instance-wide, so this signal cannot be
  hidden.
- **Upgrading from a version without project access** kept everyone's access:
  every existing editor and viewer became a member of every existing non-demo
  project with the same role, and each existing demo kept only its creator.

## Members

**Settings → Members** lists everyone with access to the workspace. The roster
itself is visible to any authenticated user; **only owners** see the per-row
role dropdown and can change roles. Non-owners see read-only role chips.

Each row shows the member's name (or email), email, join date, and role
(**Owner** / **Editor** / **Viewer**). Owners cannot change their *own* role from
this screen — use another owner account if you need to step down, and remember
the last-owner guard above.

Granting **Owner** and every demotion (Owner → Editor, anything → Viewer) ask
for confirmation first and say what changes; a promotion short of Owner
(Viewer → Editor) applies at once. If a change is refused, the error appears on
that member's row.

:::warning Registration ships open — close it once your team has accounts
Self-service registration used to be the only way to add a person, which is why
it ships **open by default**. You can now **invite people directly** instead
(see below), so closing registration no longer blocks onboarding. On an open
instance
anyone who can reach the URL can sign up, join as **editor**, and immediately
read the whole tracking plan and this member roster — and **edit any shared
project**. Data source connection details (host, port, username) are owner-only.
Warehouse table and column names are available to editors authoring scans and
metrics. For a source owned by a project, the editor must also be allowed to
edit that project; workspace-global sources remain shared. A failed connection
test distinguishes an authentication failure from a network failure, and
renaming a source to an existing name returns a conflict error.
Decide the policy before you expose the instance; see
[Security & access](#security--access) and
[Security & Hardening](../run/security.md#self-service-registration).

### Invite a member

The recommended way to add someone, and the only one that works without opening
the instance to the world:

1. **Settings → Members → Invite a member**. Enter their email and pick a role.
   Picking **Owner** shows what the role grants, and creating an owner invite
   asks for confirmation — whoever opens that link administers the instance.
2. **Copy the link it returns.** It is shown once and cannot be retrieved
   afterwards — send it however you like (the instance may have no SMTP). The
   panel names the role and stays until you **Dismiss** it; creating another
   invite before the link was copied asks first, since the uncopied link would
   be lost.
3. They open the link, set a password, and land in the workspace at the role you
   chose. Only a link the server rejects (used, expired or revoked) shows
   **This invite link no longer works**; if the page could not reach the server
   or it failed, it shows **Could not check this invitation** with **Try again**,
   so a network blip does not read as a dead link.

Owner only, and only from a signed-in browser session — an API key cannot mint
an account whatever its scope. The link works a single time, expires after 72
hours, and is bound to the address you typed, so it cannot be redeemed into a
different identity. Pending invitations are listed under **Settings → Members**
and revoking one kills its link immediately. Inviting the same address again
invalidates the previous link.

This works while registration is **Disabled** — that is the point of it.

Adding a member while registration is **Open**: they can also just register
themselves at the sign-in page, and you adjust their role from
**Settings → Members**.

While registration is disabled the sign-in page shows no sign-up form at all,
and `POST /auth/register` is refused with a `403` that tells the visitor to ask
an owner. The one exception is an instance with **no users at all** — that first
registration always works and becomes the owner, so a fresh or reset deploy can
always be claimed. Registration is also rate-limited (see
[Security & access](#security--access)).
:::

## Profile & account security

These two sections live under **Settings → Account** and apply only to the
signed-in user.

### Profile

**Settings → Profile** shows your details pulled from the authenticated account:
**Name**, **Email**, **Role** ("Set by a workspace owner") and the **Timezone**
your timestamps follow, which is read from the browser. All of them are
read-only here.

What is not built yet — avatar upload, editing your name, date format and start
of week, and personal notifications — is listed in one **Coming later** card
with no controls. Alerts and digests are addressed to a project's destinations
under Alerting, not to a person.

### Account security

**Settings → Password & sessions** has one working control: **Email me a reset link**. It
runs the same password-reset flow as the sign-in screen's **Forgot your
password?** link (`/auth/password-reset/request`) for your signed-in address.
When the instance cannot send email, the button is disabled from the start,
for everyone: an owner gets a **Set up email** link beside it, and everyone else
reads "Ask an owner to set it up."
Your current password keeps working until you choose a new one from the link.

Changing the password in place, two-factor authentication and a list of
signed-in devices are not built; the page lists them under **Coming later**.
To invalidate a user's sessions today, change their role (which deletes their
sessions) or have them log out. Sessions also expire automatically after the
configured TTL (`session_ttl_hours`, default **168 hours / 7 days**).

## API keys & governance

API keys are long-lived bearer tokens for non-browser clients — LLM agents and
CLI scripts. They are managed **per user** at **Settings → API keys** (backed by
`/api/v1/me/api-keys`). Creation and revocation require an interactive session;
a Bearer API key cannot manage keys. A user only ever sees and revokes **their own** keys;
there is no cross-user key administration, even for owners.

### Creating a key

The create form (and the `POST /api/v1/me/api-keys` endpoint) takes:

| Field | Notes |
| --- | --- |
| **Name** | Required, 1–100 chars. A human label (e.g. `claude-agent`). |
| **Scope** | `read` (default) or `write`. See below. |
| **Project** | Optional. Bind the key to a single project by slug, or leave as **All projects** for full account reach. |
| **Expires in (days)** | Optional, 1–3650. Blank means **no expiration**. |

**Scopes:**

- **`read`** — read/query operations only. Mutation surfaces return `403 API key
  has read-only scope`; a few complex read-only queries use `POST` with a JSON
  body and remain available.
- **`write`** — full editor-level access, *still subject to the owning user's
  role*. A write key is useless beyond reading if its owner is a viewer.
  Creating a write-scoped key itself requires the creator to be **editor or
  above** (a viewer cannot mint a write key, and the UI hides the option).

**Project binding** is orthogonal to scope. A project-bound key authenticates
**only** `/projects/{slug}/...` routes for that one project; any other project
(`403 API key is not authorized for this project`), or any instance-wide route
(`/me/...`, `/users`, ...), is rejected (`403 API key is scoped to a single
project`). Use it to fence an agent into a single project. Unbound keys keep
full cross-project reach.

:::tip The token is shown exactly once
On creation, the full token is returned a single time and displayed in a
"Copy your API key now" dialog. Copy it immediately — only a non-secret
**prefix** (`tk_<scope-letter>_<first chars>`, e.g. `tk_r_…` / `tk_w_…`) is
stored and shown afterward. Server-side, only a SHA-256 **hash** of the token is
persisted, so a database dump cannot replay tokens.
:::

### Governing keys

- **Inventory.** The **All keys** card lists every key with its name, prefix,
  scope chip (`read`/`write`), bound project (or "All projects"), and
  last-used / status (`never used`, `used <date>`, `expired`, or `revoked`).
  Its heading counts usable keys separately from dead ones — "7 active · 3
  revoked or expired" — so the summary never files a revoked token as live.
  `last_used_at` is updated on use but throttled to at most once per ~60s to
  avoid write amplification on the auth hot path.
- **Revocation is immediate and soft.** Revoking sets `revoked_at`; the key
  stops authenticating at once (callers begin receiving `401`). Revoke is
  idempotent. Creating and revoking keys are recorded in the **audit log**
  (`api_key.create` / `api_key.revoke`).
- **Expiry** is enforced server-side: an expired key authenticates as if
  unknown (`401`).

For how clients present these tokens (the `Authorization: Bearer <token>`
header) and the endpoints they unlock, see the
[Agent API guide](../integrate/agent-api-guide.md).

:::note What a key cannot reach, whatever its scope
Owner-only endpoints require an **interactive owner session**, so an API key is
`403` on them even when its owner is an owner. The single exception is the
[metrics replay](../integrate/agent-api-guide.md#replaying-metrics): it only
re-runs SQL an owner already authored, so an owner's `tk_w_` key may trigger one.
In practice that means
**connecting a data source and inviting a member are browser-only steps** — no
CLI, script or agent can do them for you, which is why
[`tripl install`](../run/cli.md#tripl-install) hands you a URL at the end instead
of finishing the job. The [Operator CLI](../run/cli.md) documents which of its
commands need a key at all: `install` and `upgrade` need none, the diagnostics
need `tk_r_`, and three verbs need `tk_w_` behind an editor or owner.
:::

## Instance settings (owner only)

**Settings → Instance** is visible only to owners. A non-owner who opens an
instance page from a link sees the section's title, a lock notice ("Owner role
is required to view or change instance-level settings. Ask an owner, or go to
Profile.") and a **Go to Profile** link, and the API rejects them (`GET`/`PATCH`/`PUT /api/v1/settings` all require an owner
session). It exposes a curated subset of the server configuration as overrides
stored in the database.

### How overrides work

Each editable field resolves as **database override → environment value**. The
defaults come from the environment / `Settings` object; saving a value in the UI
writes an override row, and a per-section **Reset** removes the override so the
field falls back to its env value. The **Reset** card appears only while the
section has overrides to clear. A field whose value came from somewhere other
than the built-in default carries a small **source badge**; a legend above the
fields explains that an unmarked row is at its built-in default:

- **Override** — a row exists in this instance's settings table. A section
  **Reset** clears it. An override whose value happens to equal the default
  still reads *Override*, because there is a row to clear.
- **Env** — no override, and the value differs from the built-in default, so
  something delivered it: an environment variable or a `.env` line.
- **No badge** — the value equals the built-in default. That means *either*
  nothing was delivered for this setting, *or* what was delivered happens to
  match the default. From inside the process the two are indistinguishable, and
  a normalising validator can fold a delivered value onto the default the same
  way (`LOG_LEVEL=info` becomes `INFO`). The legend says so.

The asymmetry is the point when you are verifying a deployment: a field badged
**Env** *is* evidence that a variable reached the container. An unmarked field
is *not* evidence that it did not.

Fields that depend on a master switch — AI, Search embeddings, HSTS, Rate
limiting, and the inactive storage backend — fade while that switch is off.
They stay editable, but they have no effect until the switch is on.

:::note Secrets are write-only
Secret fields — the AI API key, the search-embedding API key, and the SMTP
password — are **encrypted at rest** (Fernet) and never returned to the browser.
The UI never shows a stored secret's value: at most a `Configured` /
`Not configured` placeholder and the same **Override** / **Env** badge (or none,
at the default) the other rows carry, which says where the secret came from and never what
it is. Leave the field blank to keep the existing value, type a new value to
replace it, or use **Clear** to remove the override. Encryption requires `ENCRYPTION_KEY` to be set
(see [Configuration](../run/configuration.md)).
:::

#### When changes take effect

Sections apply at one of two times, and each section says which above its fields:

**Use-time (no restart).** The **Runtime** (query limits, app base URL),
**Email**, and **AI** sections are resolved override → env value on each call, so
edits apply immediately. If the worker cannot read the settings table, it falls
back to environment values and increments `tripl_settings_read_failures_total`
with `section=ai`, `email`, or `runtime`. Alert on this metric so a degraded
delivery does not go unnoticed.

**Restart-time (next deploy).** The **Security & access** (except
**Registration**, which applies immediately), **Storage**, and
**Observability** sections are consumed by parts of the server that are wired
once at process start — the middleware stack (CORS, security headers, the
session cookie), the auth rate limiters, the photo storage backend, logging, and
the metrics route. At startup the saved overrides are **applied onto the running
configuration** before any of those are built, so they take effect on the **next
restart/redeploy**. You no longer need to also set the matching environment
variable; the env var is just the default the override replaces.

Each section in the UI states its own answer rather than leaving you to work it
out: Runtime, Email and AI say a saved override applies to the very next
request or scan task, the next message tripl sends, and the next AI call
respectively — no restart needed. Security & access, Storage and Observability
say a saved override applies after the next restart (of the API, and of the
workers too for Storage and Observability), with Security & access naming
**Self-service registration** as the one field of its own that applies to the
very next signup attempt. Every note also repeats that unset fields fall back to
environment variables.

:::note A running process won't pick these up live
Because these sections are read once at boot, editing them does **not** change a
process that's already running — restart the API (and worker) container for the
new values to take effect. A bad value (e.g. a CORS origin that locks you out)
is recovered the same way you set it: edit it back, or clear the override, and
restart. The matching environment variable still works as the baseline default —
see [Configuration](../run/configuration.md).
:::

The sections, with the fields each exposes:

### Runtime

Core server configuration.

- **App base URL** — used in emails, webhooks and the ingest endpoint
  (`app_base_url`). Like the rest of this section it is read per request, so
  correcting it here fixes those links immediately, with no restart. It does
  **not** reach API clients: `/openapi.json` publishes no `servers` block, so a
  generated client and the "Try it out" panel in `/docs` address whichever origin
  they fetched the spec from — see [Base URL](../integrate/agent-api-guide.md#base-url).
  Do check the value anyway: an address left over from setup is invisible in the
  UI and surfaces only as password-reset and alert links that open the wrong host
  (or nothing at all) for whoever clicks them.
- **Scan row limit default** — default warehouse row cap for scans
  (`scan_row_limit_default`, default 50,000).
- **Metrics row limit default** — default row cap for metric queries
  (`metrics_row_limit_default`, default 100,000).

### Email

SMTP transport for alert delivery, scheduled digests, invitations and
password-reset links. Leaving **SMTP host** blank disables all of them.

- **SMTP host** (`smtp_host`)
- **Port** (`smtp_port`, default 587) — has to agree with **Security** below.
- **SMTP username** (`smtp_username`)
- **SMTP password** (`smtp_password`, secret — write-only)
- **Security** (`smtp_security`, default STARTTLS) — `starttls` connects in the
  clear and upgrades after the greeting (ports 587/2525); `implicit_tls` wraps
  the socket in TLS before sending anything (SMTPS, port 465); `none` stays
  plaintext. Mismatching this with the port does not raise an error, it stalls:
  the client waits for a greeting that never arrives. Replaces the old **Use
  TLS** switch, which could only ever mean STARTTLS and so left a 465 relay
  unreachable however it was set.
- **Default From address** (`smtp_from_address`) — used when a destination
  doesn't override it, and **required** for password-reset mail: without it a
  reset link is minted and then dropped. A display name is allowed
  (`Tripl Alerts <no-reply@example.com>`), and the address inside it is checked
  when you **save** rather than hours later by a failed alert. The
  per-destination From: override accepts exactly the same values, so anything
  this field takes can also be set on a single destination.
- A **Send test email** card that sends one message to your own address and
  shows what the relay answered. Its button stays disabled until an SMTP host
  and a default From address are saved, and the card reads "Uses the saved settings, so save your changes
  first." — otherwise you are testing what is still stored rather than what is
  on screen.

### AI

Powers anomaly explanations, schema/description suggestions, and the assistant.
Disabled by default because plan content is sent to the configured provider when
enabled.

- **Provider:** AI enabled (`ai_enabled`), Base URL (`ai_base_url`, default
  `https://api.openai.com/v1`; http and localhost endpoints such as a local LLM
  are allowed), Model (`ai_model`, default `gpt-4o-mini`), **AI API key**
  (`ai_api_key`, secret), and a **Test AI** button that runs a live connection
  check against the provider. It is disabled while AI is off in the saved
  settings, and while no API key is stored (the `OPENAI_API_KEY` environment
  fallback counts as one).
- **Generation:** Timeout seconds (`ai_timeout_seconds`, default 30), Max output
  tokens (`ai_max_output_tokens`, default 700), and three editable system
  prompts — **Describe prompt**, **Ask prompt**, **Alert explanation prompt** —
  which fall back to built-in defaults. A prompt that differs from its default
  shows **Restore default**, which puts the built-in text back in the editor
  (read from `GET /api/v1/settings/ai/defaults`); save to keep it.
- **Search embeddings:** toggle (`search_embeddings_enabled`), read-only
  **Embeddings base URL** (`search_embedding_base_url`, default
  `https://api.openai.com/v1`, env only), read-only
  **Embedding dimensions** (`search_embedding_dimensions`, default 1536, env
  only), provider (`search_embedding_provider`, default `openai`), model
  (`search_embedding_model`, default `text-embedding-3-small`), and **Embedding
  API key** (`search_embedding_api_key`, secret).

  The base URL is the endpoint every indexed event name, description and field
  value is POSTed to. It is shown precisely so an operator can confirm where
  that text is going without reading the source, which is why it carries a
  source badge like everything else: **Env** means something delivered
  `SEARCH_EMBEDDING_BASE_URL` to this container. It is reported, never accepted —
  the vectors already in the index were written against whatever endpoint
  produced them, and similarity across two embedding spaces is meaningless, so
  changing it is a re-index and a deploy rather than a setting.

If no key is set on either AI secret field, the server falls back to the
`OPENAI_API_KEY` environment value when resolving the effective key.

### Security & access

Authentication and network policy for everyone on the instance.

- **Registration** (`registration_mode`, default **Open**) — whether strangers
  can create their own account. **Open** allows self-service signup: anyone who
  can reach this instance creates an account, joins as **editor**, and can
  immediately read the whole tracking plan and the member roster, and **edit any
  shared project**. Each data source's name, type and health are visible to
  everyone; its connection details (host, port, username, whether a password is
  set) are owner-only, and the password itself is never returned to anybody.
  **Disabled** refuses `POST /auth/register` with a `403` and hides
  the sign-up form on the sign-in page. It defaults to Open only because there
  is no invite or owner-creates-user flow yet, so a closed instance cannot
  onboard anyone; close it once your team has accounts. See
  [Members](#members) for the onboarding flow.
- **Sessions:** Session cookie name (`session_cookie_name`, default
  `tripl_session`), Session TTL hours (`session_ttl_hours`, default 168), Secure
  cookie (`session_cookie_secure`).
- **Network & headers:** CORS allow origins (`cors_allow_origins`, comma-
  separated), Security headers (`security_headers_enabled`), HSTS
  (`hsts_enabled`) and HSTS max age (`hsts_max_age_seconds`), Content Security
  Policy (`content_security_policy`).
- **Rate limiting:** master toggle (`rate_limit_enabled`), Login limit
  (`rate_limit_login_per_minute`, default 5/min), Register limit
  (`rate_limit_register_per_hour`, default 3/hour), and **Trust
  X-Forwarded-For** (`rate_limit_trust_forwarded_for`).

:::tip Registration is the one field here that applies immediately
Everything else in this section is read once at process start (see
[When changes take effect](#when-changes-take-effect)), but **Registration** is
resolved on every signup attempt — closing the door must never wait for a
redeploy. The `REGISTRATION_MODE` env var remains the default the override
replaces.
:::

:::danger Trust X-Forwarded-For only behind a trusted proxy
`rate_limit_trust_forwarded_for` defaults to **false**. Enable it only when a
trusted proxy/load balancer sits in front and overwrites `X-Real-IP` on every
request. On a directly-exposed API, a raw `X-Forwarded-For` is
attacker-controlled — trusting it lets an unauthenticated caller rotate the
header per request and bypass the rate limit entirely. Set it either way: the
`RATE_LIMIT_TRUST_FORWARDED_FOR` env var and the **Security & access** override
both work, and the override wins. Like the rest of this section it is read once
at startup, so either route takes effect on the next restart.
:::

### Storage

Where event photos are persisted (`photo_storage_backend`: **Local filesystem**
or **Google Cloud Storage**).

- **Backend:** Photo storage backend, Photo max size (`photo_max_size_mb`,
  default 10), Allowed MIME types (`photo_allowed_mime`).
- **Local filesystem:** Local photo directory (`photo_local_dir`, default
  `./var/photos`). In the shipped image the default resolves to
  `/app/var/photos`, which `compose.yaml` mounts as the `photos` volume. Point
  it elsewhere only at another mounted directory the image's `app` user can
  write, or uploads are lost when the container is recreated.
- **Google Cloud Storage:** GCS bucket (`gcs_photo_bucket`), GCS public URLs
  (`gcs_photo_public`), GCS credentials path (`gcs_photo_credentials_path`,
  blank falls back to Application Default Credentials), Signed URL TTL
  (`gcs_photo_signed_url_ttl_seconds`, default 3600). Credentials that cannot
  sign URLs serve photos through the authenticated API endpoint instead.

A branch merge that removes an uploaded screenshot from main deletes its file
once no attachment row on any branch uses it. A file stored under a backend
other than the one configured now is left in place, with a warning in the log.

### Observability

How tripl reports its own health.

- **Logging:** Log level (`log_level`: DEBUG/INFO/WARNING/ERROR/CRITICAL,
  default INFO), JSON logs (`log_json`), Request ID header
  (`request_id_header`, default `X-Request-ID`).
- **Metrics & tracing:** Prometheus metrics (`prometheus_metrics_enabled` —
  exposes `/metrics`), OTLP endpoint (`otel_exporter_otlp_endpoint` — setting a
  non-empty value opts the API and worker into OpenTelemetry auto-
  instrumentation; the production image includes the required packages), OTEL
  service name (`otel_service_name`, default `tripl`).

### System (read-only)

A health/build panel. Each tile shows **Configured** or **Unset** for: Debug
mode, Database URL, Sync database URL, RabbitMQ URL, Redis URL, Encryption key,
and the OpenAI fallback key. One further tile, **Schema revision**, reports the
Alembic revision this instance's database is actually stamped with — the
`version_num` in its `alembic_version` table — in one of four states:

The first request in each API process loads the shipped migration head in a
background thread, so reading the migration files does not block other API
requests on the event loop.

- **the revision string**, in a success tone, when it equals the migration head
  this build ships. The database is at the newest migration the running image
  knows about.
- **the revision string**, in a danger tone, when it does not. The tile names
  the head this build ships, so you can see both revisions — but not which way
  round they are. Nothing here compares the applied revision against this
  build's migration graph, so this one state covers both causes described below:
  a migrate step that never ran here, and a rollback onto a database a newer
  release already upgraded.
- **the revision string**, in a warning tone, when the database is stamped but
  this build's own migration head could not be determined. The revision is
  reported as read; only the comparison is missing.
- **Unknown**, in a warning tone, when `alembic_version` itself could not be
  read: no such table, no row, or the database was unreachable. The tile still
  names the head when it has one. Unknown is reported as unknown rather than
  guessed at, so an instance whose revision merely could not be read is never
  painted as one whose migrations were skipped.

Nothing here is editable — it reflects the process's environment, plus this one
value read live from the database each time the page loads — so you can confirm
the instance is wired correctly and that its schema is where the build expects.
The comparison is a plain equality, so any mismatch shows the same danger state
with the build's head named beside it. The usual cause is a database that never
had `alembic upgrade head` run against it. The other is a rollback: the migrate
one-shot is **forward-only** and pulling an older image does not revert a schema
change, so an older build against an already-upgraded database mismatches too —
see [Migrations are forward-only](../run/runbook.md#rollback--downgrade) in the
runbook.

## Related pages

- [Configuration](../run/configuration.md) — the full environment-variable
  reference behind these settings.
- [Security](../run/security.md) — hardening the instance (secrets, HTTPS, CORS).
- [Deployment](../run/deployment.md) — running the instance (compose / GHCR
  image) and what a restart picks up.
- [Operator CLI](../run/cli.md) — `tripl install` / `tripl upgrade` for the host,
  and `tripl doctor` for checking the instance once it is up.
- [Agent API guide](../integrate/agent-api-guide.md) — using API keys from
  agents and scripts.
- [Troubleshooting](../use/troubleshooting.md) — common operational issues.
