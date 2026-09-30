# Agent API Guide

This guide describes the recommended way for external LLM agents and CLI scripts to consume the tripl API.

## Recommendation

Use the existing FastAPI OpenAPI contract plus this guide as the primary agent integration path.

- Machine-readable contract: `GET /openapi.json`
- Interactive contract browser: `GET /docs`
- Base API prefix: `/api/v1`

tripl now ships a first-party MCP server (`tripl-mcp`) that wraps this API in a curated toolset for MCP-capable agent runtimes — see [MCP Server](./mcp-server.md) for setup. This guide remains the raw REST contract underneath it: every MCP tool calls the endpoints described here with the same API-key auth, project fencing, and branch rules. Use the MCP server when the agent runs in an MCP-capable runtime; use raw OpenAPI plus this guide for direct HTTP integrations, scripts, and anything the curated toolset does not cover.

## Base URL

The published document carries no `servers` block, by design. A client therefore
resolves every path against the URL it fetched the spec from: an instance reached
at `https://tripl.example.com/openapi.json` is called at
`https://tripl.example.com/api/v1/...`, and the same build reached at
`http://localhost:8000` in development is called there. Nothing to configure, and
no server-side setting can point your client at a different host than the one you
already reached.

Two consequences worth knowing:

- In `/docs`, **Try it out** calls the origin the page is open on. That is a
  same-origin request, so it works regardless of the instance's CORS allow-list.
- Code generators that insist on an absolute base URL substitute their own
  placeholder (often `http://localhost`) when `servers` is absent. Set your
  origin on the generated client's configuration instead of expecting the spec to
  carry it. The same applies to the committed `backend/openapi.json` in the
  repository, which is the same document with no retrieval URL to resolve against.

### Organization-qualified paths {#org-paths}

Every project lives in one organization. Each path under `/api/v1/projects`,
`/api/v1/activity`, `/api/v1/audit`, `/api/v1/data-sources`, `/api/v1/users` and
`/api/v1/me` is also reachable with the organization spelled out:

```text
GET /api/v1/projects/checkout/event-types
GET /api/v1/orgs/default/projects/checkout/event-types   # same route, same response
```

The org-qualified form is served by the same route, so the OpenAPI document lists
only the short form. The rules:

- **API keys** belong to one organization. A key used under a different
  `/orgs/{org}/` answers `404 Organization not found`, as does an organization that
  does not exist; an unauthenticated request answers `401` either way.
- **Session users** must be a member of the organization named in the path.
- **Without an org in the path**, a self-hosted instance acts in its default
  organization (slug `default`), exactly as before. On a hosted instance the short
  form works only for a user in exactly one organization; anyone else gets
  `400 Organization required` and must use `/api/v1/orgs/{org}/...`.
- `/api/v1/settings`, `/api/v1/project-templates` and `/api/v1/auth` are not
  org-qualified: `/api/v1/orgs/{org}/settings` is reserved for per-organization
  settings and answers `404` until those routes exist.
- `/api/v1/orgs/{org}`, `/members`, `/members/{user_id}` and
  `/transfer-ownership` are real routes of the [organization API](#organizations),
  not rewritten ones.

### Organizations {#organizations}

The organization management API. Everything under `/api/v1/orgs/{org}` answers
`404 Organization not found` to anyone who is not a member of `{org}`, to an API
key of another organization, and for an organization being deleted — the same
answer as for a slug that does not exist, and always before any `403`.

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs` | any account | Your organizations, with your role and the organization's `status` (`active` or `suspended`) in each. An API key lists only its own organization. |
| `POST /api/v1/orgs` | self-hosted: platform admin; hosted: any account; browser session | `{"slug", "name"}`; the creator becomes the owner. `409` when the slug is taken, `422` for an invalid or reserved slug. |
| `GET /api/v1/orgs/{org}` | any member (or its key) | `id`, `slug`, `name`, `role`, `status`, `is_default`, `default_project_role`, `created_at`. |
| `PATCH /api/v1/orgs/{org}` | owner or admin, browser session | `{"name"?, "default_project_role"?}`: a rename, and/or the default access to projects (`none`, `viewer` or `editor`; `owner` is `422`), audited as `org.update` with before and after. The slug is permanent; sending one is `422`. |
| `DELETE /api/v1/orgs/{org}` | owner, browser session | `{"confirm_slug": "<slug>"}`. `202`, then a background job purges the organization. The default organization is `400`. |
| `GET /api/v1/orgs/{org}/members` | any member (or its key) | Members with their organization role; `limit` / `offset`. |
| `PATCH /api/v1/orgs/{org}/members/{user_id}` | owner or admin, browser session | `{"role": "owner" \| "admin" \| "member"}`. Only an owner manages owners; the last owner cannot be demoted (`400`). |
| `DELETE /api/v1/orgs/{org}/members/{user_id}` | owner or admin, browser session | Removes the membership, the user's project memberships in the organization and their group memberships in it, revokes their keys bound to it, and deletes their single sign-on identities for it. |
| `POST /api/v1/orgs/{org}/transfer-ownership` | owner, browser session | `{"user_id"}`: that member becomes an owner, the caller an admin. |
| `GET /api/v1/orgs/{org}/groups` | any member (or its key) | The organization's groups by name: `id`, `name`, `description`, `member_count`, `created_at`, `updated_at`. |
| `POST /api/v1/orgs/{org}/groups` | owner or admin, browser session | `{"name", "description"?}`. `201` with the group and its (empty) `members`. `409` when the organization already has a group of that name (ignoring case); `422` for a blank name or a NUL character. |
| `GET /api/v1/orgs/{org}/groups/{group_id}` | any member (or its key) | The group with `members`: `user_id`, `email`, `name`, `added_at`. |
| `PATCH /api/v1/orgs/{org}/groups/{group_id}` | owner or admin, browser session | `{"name"?, "description"?}`; an omitted field is unchanged, `null` is `422`. `409` on a name clash. |
| `DELETE /api/v1/orgs/{org}/groups/{group_id}` | owner or admin, browser session | `204`. The group and its memberships go; the members stay in the organization. |
| `POST /api/v1/orgs/{org}/groups/{group_id}/members` | owner or admin, browser session | `{"user_id"}`. `201` with the member. `404` when the user is not a member of the organization, `409` when already in the group. |
| `DELETE /api/v1/orgs/{org}/groups/{group_id}/members/{user_id}` | owner or admin, browser session | `204`; `404` when the user is not in the group. |

Single sign-on (see [the admin guide](../administer/admin-guide.md#single-sign-on))
is configured by the organization's **owners** only, from a browser session;
an admin, a member or any API key gets `403`:

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs/{org}/sso` | owner, browser session | `configured`, `protocol` (`oidc` or `saml`), `enabled`, `sso_required`, `login_url` (where members start signing in); for OpenID Connect `issuer`, `client_id`, `client_secret_configured` (the secret itself is never returned), `scopes` and `redirect_uri` (to register at the provider); for SAML `saml_idp_entity_id`, `saml_idp_sso_url`, `saml_idp_certs` (PEM, public), `saml_name_id_format`, `saml_email_attribute`, the read-only `saml_sp_entity_id`, `saml_acs_url` and `saml_metadata_url` (to register at the IdP) and `saml_cert_info` (`[{"fingerprint_sha256", "not_after", "subject"}]`). Fields never set are empty strings (`saml_email_attribute` is `null`). A save that changes the protocol, the SAML entity ID, or replaces every SAML certificate unlinks the members' SSO identities of the old provider (they confirm the link again); the unchanged values a save echoes back are not re-validated. `saml_idp_entity_id` is at most 507 characters. |
| `PUT /api/v1/orgs/{org}/sso` | owner, browser session | The whole configuration: `{"protocol"?, "issuer", "client_id", "client_secret"?, "scopes"?, "saml_idp_entity_id"?, "saml_idp_sso_url"?, "saml_idp_certs"?, "saml_name_id_format"?, "saml_email_attribute"?, "enabled"?, "sso_required"?}`. `protocol` defaults to `oidc`. The fields of the chosen protocol are required and checked; the other protocol's may be `null`. An omitted `enabled` or `sso_required` is `false`; an omitted or `null` `client_secret` keeps the stored one, which is write-only. `scopes` defaults to `openid email profile` and must include `openid`. For SAML, `saml_idp_sso_url` must be `https`, `saml_idp_certs` holds one or more PEM certificates (several while the IdP rotates its key), `saml_name_id_format` defaults to `urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress`, and `saml_email_attribute` names the attribute carrying the email when the NameID is not one. `enabled: true` needs a saved provider and at least one verified domain. Turning `sso_required` on revokes the organization's API keys that were not created from a single sign-on session of it, owners' included; `revoked_api_keys` in the response says how many. While it is on, such keys are refused with `403` and new keys are created only from a single sign-on session of the organization (an owner's password session gets `403` too). Audited as `org.sso.*`, without the secret. |
| `POST /api/v1/orgs/{org}/sso/test` | owner, browser session | OpenID Connect: fetches the issuer's discovery document and checks the issuer and endpoints: `{"ok", "message", "error_code", "authorization_endpoint", "token_endpoint", "jwks_uri", ...}`. SAML: checks that the saved certificates parse and have not expired and that the SSO URL is `https`, without contacting the IdP. A failure's `message` is a fixed text per `error_code`. Rate-limited to 10 a minute, shared with domain verification. |
| `POST /api/v1/orgs/{org}/sso/saml/metadata-import` | owner, browser session | `{"xml"}`: pasted IdP metadata (an `EntityDescriptor`). Answers `{"saml_idp_entity_id", "saml_idp_sso_url", "saml_idp_certs"}` (the HTTP-Redirect single sign-on location and the signing certificates as PEM) without saving anything; send them with `PUT` to keep them. No URL is fetched. Metadata that does not parse, carries a `DOCTYPE`, or lacks an entity ID, a redirect location or a signing certificate is `422`. |
| `GET /api/v1/orgs/{org}/sso/domains` | owner, browser session | The claimed domains: `id`, `domain`, `verified`, `verified_at`, `txt_record_name`, `txt_record_value`, `created_at`. |
| `POST /api/v1/orgs/{org}/sso/domains` | owner, browser session | `{"domain"}`, lowercased. `201` with the domain and the TXT record to publish. A domain another organization has verified is `409`. |
| `DELETE /api/v1/orgs/{org}/sso/domains/{id}` | owner, browser session | Removes the claim. |
| `POST /api/v1/orgs/{org}/sso/domains/{id}/verify` | owner, browser session | Looks up the DNS TXT record `_tripl-verification.<domain>` and marks the domain verified when it contains `tripl-verification=<token>`. Rate-limited to 10 a minute, shared with the connection test. |

In an organization with `sso_required`, a request from a browser session that
did not sign in through the organization's provider answers
`403 {"detail": "This organization requires single sign-on", "sso_start": "/api/v1/auth/sso/<org>/start"}`
(organization owners and a platform admin's read-only step-in excepted). An API
key bound to it works only if it was created from a single sign-on session of
that organization; any other key is `403`.

The audit log leaves tripl two ways (see
[Exporting the audit log](../administer/admin-guide.md#audit-export) and
[Audit webhook](../administer/admin-guide.md#audit-webhook)). Neither takes an
API key: the export is an owner's or admin's browser session, like the audit
feed, and the webhook an owner's:

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs/{org}/audit/export?format=csv\|json&from=YYYY-MM-DD&to=YYYY-MM-DD&action=` | owner or admin, browser session | A streamed download (`Content-Disposition: attachment`, `audit-<org>-<from>-<to>.<ext>`) of the organization's entries and its projects', ordered by `created_at` then `id`. `csv` is `text/csv` with every cell quoted and formula-like cells (`=`, `+`, `-`, `@`, tab, carriage return) prefixed with `'`; `json` is NDJSON (`application/x-ndjson`), one object per line. Columns: `id`, `created_at`, `org_slug`, `project_slug`, `branch_name`, `user_email`, `action`, `target_type`, `target_id`, `target_name`, `payload`. The range is `from` **included**, `to` **excluded** (both read in UTC; a bare date is midnight), so to include a whole last day send the day after it. `to` must be after `from` and at most 366 days later, else `422`. `action` is optional. Rate-limited (`429`). Audited as `org.audit_export`. |
| `GET /api/v1/orgs/{org}/audit/webhook` | owner, browser session | Always `200`: `configured`, `url`, `enabled`, `secret_configured` (the secret itself is never returned), `last_success_at`, `last_error`, `last_error_at`. With no webhook, `configured` is `false`, `url` is `""` and `enabled` `false`. |
| `PUT /api/v1/orgs/{org}/audit/webhook` | owner, browser session | `{"url", "enabled"}`. The URL must be `https`, and on a hosted instance a public address (`422` otherwise). Answers the webhook's fields; the call that creates it also generates the signing secret and returns it once, in `secret`. Rate-limited with `test` (10 a minute, `429`). |
| `DELETE /api/v1/orgs/{org}/audit/webhook` | owner, browser session | Removes the webhook; nothing more is sent. |
| `POST /api/v1/orgs/{org}/audit/webhook/rotate-secret` | owner, browser session | The webhook's fields (as `GET`) plus `secret`: a new signing secret, returned once. The old one stops working at once. `404` when there is no webhook. |
| `POST /api/v1/orgs/{org}/audit/webhook/test` | owner, browser session | Sends a synthetic `audit.webhook_test` event now: `{"ok", "status_code", "error"}`. At most 15 seconds; rate-limited (10 a minute, `429`). |
| `GET /api/v1/orgs/{org}/audit/webhook/deliveries?status=&limit=` | owner, browser session | Recent deliveries, newest first: `id`, `audit_log_id`, `action`, `status` (`pending`, `sent`, `failed`, `dead`), `attempts`, `next_attempt_at`, `last_error`, `created_at`, `sent_at`. |

Each delivery is a `POST` of one entry as JSON with `X-Tripl-Event-Id` (the
entry's `id`), `X-Tripl-Timestamp` (Unix seconds) and
`X-Tripl-Signature: sha256=<hex HMAC-SHA256 of "t=<timestamp>.<raw body>">`
(the literal `t=`, then the `X-Tripl-Timestamp` value, a dot and the body bytes).
Webhook changes are audited as `org.audit_webhook.*`, without the secret.

A group id of another organization answers `404 Group not found`, like an id
that does not exist. Every group change is audited (`org.group.create`,
`org.group.update`, `org.group.delete`, `org.group.member_add`,
`org.group.member_remove`). A group carries `managed_by_scim`: `true` when the
organization's identity provider created it, or has written to it, over SCIM.
Renaming, deleting or changing the members of such a group here answers `409`;
its description stays editable.

SCIM provisioning (see [the admin guide](../administer/admin-guide.md#scim)) is
set up by the organization's **owners** only, from a browser session; an admin,
a member or any API key gets `403`:

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs/{org}/scim/tokens` | owner, browser session | The SCIM tokens, revoked ones included: `id`, `prefix`, `created_at`, `created_by_email`, `last_used_at`, `revoked_at`. The token itself is never returned here. |
| `POST /api/v1/orgs/{org}/scim/tokens` | owner, browser session | Creates a token: `{"id", "prefix", "token", "created_at"}`. `token` (starting `tripl_scim_`) is shown only in this response. Audited as `org.scim.token_create`. |
| `DELETE /api/v1/orgs/{org}/scim/tokens/{id}` | owner, browser session | Revokes the token; the identity provider's next request with it is refused. Audited as `org.scim.token_revoke`. |
| `GET /api/v1/orgs/{org}/scim/config` | owner, browser session | `{"base_url", "admin_group_id", "admin_group_name", "active_tokens"}`: the SCIM base URL to give the identity provider, the group whose members are made admins (`null` for none), and how many unrevoked tokens there are. |
| `PUT /api/v1/orgs/{org}/scim/config` | owner, browser session | `{"admin_group_id": "<group id>" \| null}`. The group must belong to the organization. |

The SCIM 2.0 protocol itself is served at `/scim/v2/{org}` (outside
`/api/v1`) for the identity provider: `ServiceProviderConfig`, `ResourceTypes`,
`Schemas`, `Users` and `Groups`. It takes only
`Authorization: Bearer tripl_scim_…` of that organization; sessions and API
keys are refused there, and a SCIM token works nowhere else. Agents and
scripts should use the `/api/v1` routes above instead.

API keys never manage an organization: every write above answers `403` to a
key, whatever its scope. Invitations into an organization are
`POST /api/v1/orgs/{org}/users/invitations` (owner or admin, browser session).

`GET /api/v1/auth/me` with an API key also returns `org` (the key's organization
slug) and `api_key_scope` (`read` or `write`); `tripl whoami` prints them. For a
browser session both are `null`. A project-bound key cannot call `/auth/me`
(`403`).

### Platform console {#platform-console}

The instance operator's API, under `/api/v1/platform`. Every route takes a
**platform admin's browser session**: an API key is `403 Platform admin session
required` whatever its scope or owner, and anyone else is `403 Platform admin
required`. Agents cannot use it; it is listed so a client can tell it apart.
Organizations are named by slug; nothing here returns project content. See
[Platform console](../administer/admin-guide.md#platform-console) for the
rules behind each action.

| Method and path | What |
|---|---|
| `GET /api/v1/platform/orgs` | `q` (name or slug), `status` (`active`, `suspended`, `deleting`), `limit`, `offset`. `{"items", "total"}`; each item: `id`, `slug`, `name`, `status`, `created_at`, `suspended_at`, `suspended_reason`, `member_count`, `project_count`, `owner_emails`. |
| `GET /api/v1/platform/orgs/{org_slug}` | The same fields plus `members` (`email`, `name`, `role`) and `projects` (`slug`, `name`, `created_at`). |
| `POST /api/v1/platform/orgs/{org_slug}/suspend` | `{"reason"}` (required, 1 to 500 characters, no NUL). The organization answers `403 This organization is suspended` to its members and keys until unsuspended. `409` for an organization being deleted and for the default organization (in any `DEPLOYMENT_MODE`). A platform admin's read-only step-in may still read it. Audited as `org.suspend` in the organization. |
| `POST /api/v1/platform/orgs/{org_slug}/unsuspend` | Back to `active`. `409` for an organization being deleted. Audited as `org.unsuspend`. |
| `POST /api/v1/platform/orgs/{org_slug}/step-in` | `{"reason", "ttl_minutes"?}`: reason 1 to 500 characters, `ttl_minutes` 5 to 240 (default 60). `{"id", "org_slug", "expires_at"}`. Starts a read-only step-in into an active or a suspended organization (`409` for one being deleted); a second step-in to the same organization supersedes the first. Audited as `platform.step_in` in the organization. |
| `POST /api/v1/platform/step-ins/{id}/end` | Ends one of your step-ins now; audited as `platform.step_in_end`. A step-in that runs out is recorded as `platform.step_in_end` with `expired: true`, lazily, the first time a request or a listing sees it. |
| `GET /api/v1/platform/step-ins` | Your step-ins; `active=true` keeps the unexpired, un-ended ones. |
| `GET /api/v1/platform/users` | `q` (email or name), `limit`, `offset`. Each item: `id`, `email`, `name`, `is_platform_admin`, `email_verified`, `created_at`, `org_count`. |
| `POST /api/v1/platform/users/{user_id}/platform-admin` | `{"grant": true \| false}`. `409` when revoking the last platform admin or yourself. Audited as `platform.admin_grant` / `platform.admin_revoke`, with no organization. |

During a step-in the admin acts in that organization as a `member` with the
`viewer` role on every project, from the browser session only. Any request in
it other than `GET`, `HEAD` and `OPTIONS` answers `403 Step-in is read-only`,
except `POST /api/v1/projects/{slug}/anomalies/signals/query` and
`POST /api/v1/projects/{slug}/events/window-metrics`. `GET /api/v1/auth/me`
returns the caller's active step-ins as `active_step_ins`
(`[{"org_slug", "expires_at"}]`).

A suspended organization answers `403 This organization is suspended` to every
request that acts in it, from a session or a key; `GET /api/v1/orgs` still lists
it, with `status: "suspended"`.

## MCP Server

For agents running in MCP-capable runtimes (Claude Code, Claude Desktop, and other MCP clients), `tripl-mcp` packages a curated read/write toolset on top of this API: stdio and streamable-http transports, `readOnlyHint` annotations on read tools, `tk_w_` key requirements on write tools, a mandatory `branch_id` on plan-mutating tools, and a `TRIPL_MCP_ALLOW_MAIN` gate that keeps agents off the main branch by default. Installation, transport configuration, and the full tool list live in [MCP Server](./mcp-server.md). Everything below documents the underlying REST contract that the MCP tools share.

## Authentication

Agents should authenticate with user-issued API keys:

```http
Authorization: Bearer tk_...
```

API keys are created by an authenticated user through:

```http
POST /api/v1/me/api-keys
```

Creation payload:

```json
{
  "name": "docs-agent",
  "scope": "read",
  "expires_in_days": 90,
  "project_slug": "demo"
}
```

Scopes:

- `read`: read-only. Mutation endpoints reject it, while read/query operations
  remain available even when an endpoint uses `POST` for a complex query body.
  Use this for retrieval, search, and agent context loading.
- `write`: allowed on mutation endpoints, subject to the roles of the user behind the key. A project write still needs an editing project role (an `editor` membership, or owner/admin of the organization). Minting a `write` key needs membership of the organization.
- Owner-only security and administration routes (data sources, scan SQL, members, invitations, the audit log) require an interactive session of an organization owner or admin; an API key is `403` on them even when its user is an owner. The one exception is the [metrics replay](#replaying-metrics), which a `write` key of an org owner or admin may call. The instance operator settings (`/settings` fields for security, observability and the server) require a platform admin's session and never take a key.
- A key belongs to the organization it was minted in and acts only there; a URL naming another organization answers `404`. `GET /api/v1/me/api-keys` lists the keys of the organization the request acts in.

Project scope:

- `project_slug` binds the key to one `/projects/{slug}/...` namespace.
- Project-scoped keys cannot call instance-level routes such as `/api/v1/projects` or `/api/v1/users`.
- Omit `project_slug` only for trusted automation that must read or write multiple projects.

With `DEPLOYMENT_MODE=hosted`, an account whose email address is not verified
gets `403 Email address not verified` on every route outside `/api/v1/auth/*`,
with a session or a key. Keys are created behind that check, so in practice a
working key always belongs to a verified account.

If a Bearer token is invalid, expired, or revoked, the API returns `401`. If a valid key lacks scope or role permission, the API returns `403`. A project-bound key used on another project's slug gets `404 Project not found`, the same answer as a slug that does not exist, even when the key's user is a member of that project; instance-wide routes still answer `403` to a project-bound key.

Project membership:

- A key acts as the user who created it, so it reaches only the projects that
  user has **access** to (the key of an owner or admin of the organization
  reaches every project of it). A member's access to a project is their
  membership row there, or, without one, the organization's
  `default_project_role` (`none`, `viewer` or `editor`); a row with role `none`
  shuts them out whatever the default. On any
  other project every `/projects/{slug}/...` route answers `404`
  `Project not found`, the same answer as for a slug that does not exist, and the
  project is missing from `GET /api/v1/projects` and `GET /api/v1/activity`.
- Creating a key with `project_slug` for a project the user cannot see
  answers `404`.
- Writing needs **editor** access, by row or by the default. A viewer's key
  gets `403` on mutation routes, whatever its scope.
- Under the `none` default (every organization's until an owner or admin
  changes it) a new user sees no project. Ask the project's creator or an owner
  or admin of the organization to add the account behind your key.

### Account endpoints {#account-endpoints}

The `/api/v1/auth` routes handle sign-in and the account itself. Agents rarely
need them beyond `/auth/me`; they are listed so a client can tell them apart
from the rest of the API. The unauthenticated ones are rate-limited per client
address and answer `429` with `Retry-After` when exceeded.

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/auth/status` | anyone | `has_users`, `registration_enabled`, `email_configured`, `deployment_mode` (`self_hosted` or `hosted`) and `email_verification_required` (`true` when hosted). A hosted instance always reports `has_users: true`. |
| `POST /api/v1/auth/register` | anyone, when registration is open | `{"email", "password", "name"?}`, plus `"org_name"` and `"org_slug"`, which a hosted instance requires (`422` without them, `409` when the slug is taken) and a self-hosted one ignores. Hosted: `503 Email delivery is not configured` when the operator cannot send mail; the new account owns a new organization and is sent a verification link. |
| `POST /api/v1/auth/login` | anyone | Starts a browser session. |
| `POST /api/v1/auth/logout` | session | `204`. |
| `GET /api/v1/auth/me` | session or key | The account, including `email_verified`, `is_platform_admin` and, for a platform admin's session, `active_step_ins` (`[{"org_slug", "expires_at"}]`). |
| `POST /api/v1/auth/verify-email/request` | session | `204`. Sends a new verification link (24 hours, single use) and invalidates the earlier unused ones; a verified account gets `204` and no mail, and so does every account on a self-hosted instance, where verification is not required. `503` when the operator cannot send mail. Rate-limited to 10 an hour. |
| `POST /api/v1/auth/verify-email/confirm` | the signed-in browser session of the token's own account | `{"token"}`. `204`, and the address is verified; every other session of the account is signed out, the caller's is kept. No session: `401 Sign in to confirm your email address.` and the token stays unused. An unknown, expired or used token, or one sent to another account than the signed-in one, is one uniform `400` (the token stays unused). Hosted: an address listed in `PLATFORM_ADMIN_EMAILS` becomes a platform admin here, and only here. |
| `POST /api/v1/auth/password-reset/request` | anyone | Sends a reset link when the address has an account; the answer does not say whether it does. |
| `POST /api/v1/auth/password-reset/confirm` | anyone | Sets the new password and marks the address verified. Signs the account out everywhere and revokes all of its API keys; issue new keys afterwards. Never grants platform admin. |
| `GET /api/v1/auth/invitations/{token}` | anyone | Previews an invitation. |
| `GET /api/v1/auth/sso/discover?email=` | anyone | `{"orgs": [{"slug", "name", "login_url"}]}`: the organizations with single sign-on turned on whose verified domain the address is at. Rate-limited like `/auth/status`. |
| `GET /api/v1/auth/sso/{org}/start?next=` | anyone (a browser) | `302` to the organization's identity provider: for SAML, to its SSO URL with an unsigned `SAMLRequest` (HTTP-Redirect binding) and the state as `RelayState`. `next` is where to return afterwards and must be a relative path on this origin (`/…`, not `//…`). Start and callback share a rate limit of 20 a minute per address, apart from password sign-in. |
| `GET /api/v1/auth/sso/{org}/callback?code=&state=` | the identity provider's redirect | Finishes the sign-in and redirects: into the app with a session, to `/sso/link?ticket=…` when the address belongs to an existing account that has to confirm the link, or to `/auth?sso_error=<code>`: `sso_unavailable`, `invalid_state`, `idp_error`, `idp_denied`, `invalid_token`, `email_missing`, `email_not_verified`, `email_domain_not_allowed`, `membership_removed` (removed from the organization and not invited back), `rate_limited` or `sso_failed`. |
| `GET /api/v1/auth/sso/{org}/saml/metadata` | anyone | tripl's SAML service-provider metadata (XML): the entity ID (this URL), the ACS URL with the HTTP-POST binding, the email NameID format and `WantAssertionsSigned="true"`. Unsigned; there is no SP certificate. |
| `POST /api/v1/auth/sso/{org}/saml/acs` | the identity provider's form post (`SAMLResponse`, `RelayState`) | The SAML counterpart of the callback, with the same outcomes and the same `sso_error` codes, plus `saml_invalid` (the response failed a check: issuer, audience, recipient, destination, validity window), `idp_denied` also for a non-`Success` status, `email_missing` also when no email attribute is configured and the NameID format is not `emailAddress`, `saml_signature_invalid` (the assertion is unsigned, signed with SHA-1 or by a certificate not configured), `saml_replay` (the assertion was already used), `saml_unsolicited` (not an answer to a request tripl made: IdP-initiated sign-in is not supported) and `encrypted_assertion_unsupported`. |
| `GET /api/v1/auth/sso/link?ticket=` | anyone holding the ticket | `{"email", "org_slug", "org_name", "expires_at", "sign_in_required"}`: what confirming would link, without using the ticket; `sign_in_required` is true when this browser must first sign in to the account. `400` when the ticket is not live. |
| `POST /api/v1/auth/sso/link` | the ticket, from a browser session of the ticket's account | `{"ticket"}`: links the identity provider's account to the existing tripl account, adds the organization membership if missing, marks the address verified and replaces the session with a single sign-on session. Answers `{"next", "user"}`. Without a session of that account: `401` and the ticket stays usable. An account whose address was never verified needs no session and is taken over clean (its password, sessions and API keys are dropped). Single use, 10 minutes; `400` for an unknown, used or expired ticket; `403` for an account removed from the organization; `409` when the organization no longer uses single sign-on. |
| `POST /api/v1/auth/invitations/{token}/accept` | anyone, or the invited account signed in | Creates the account, or adds the organization to the signed-in account. Self-hosted: the new account is verified at creation. Hosted: the new account is **not** verified by the invitation and is sent a verification link to confirm, and a signed-in account must be verified first (`403`). Never grants platform admin. |

### Project members

Read who can see a project (any member may call it):

```http
GET /api/v1/projects/{slug}/members
```

```json
[
  {
    "user_id": "7c9e…",
    "name": "Ada",
    "email": "ada@example.com",
    "role": "editor",
    "added_at": "2026-09-27T09:00:00Z"
  }
]
```

`role` is the membership role: `editor`, `viewer` or `none`. A `none` row
("No access" in the app) opts an organization member out of a project the
organization's `default_project_role` would otherwise give them; members
without a row are not listed and hold the default. The project response
(`GET /api/v1/projects/{slug}`) also carries `my_role` (`owner`, `editor` or
`viewer`, never `none`: without access the project is a `404`), the caller's
effective role — `owner` for an owner or admin of the project's organization,
else their row's role, else the organization's default — and `can_mutate`.

Changing membership is limited to the organization's owners and admins and the
project's creator, and needs a browser session: every API key, whatever its
scope, gets `403` on these routes. The creator also needs an editing role: a
creator who is a `viewer` member gets `403`, and a creator who was removed from
the project gets `404`. The same applies to renaming and resetting the project;
deleting it is for owners and admins only. The user you add must be a member of
the project's organization.

```http
POST   /api/v1/projects/{slug}/members            {"user_id": "…", "role": "viewer"}
PATCH  /api/v1/projects/{slug}/members/{user_id}  {"role": "editor"}
DELETE /api/v1/projects/{slug}/members/{user_id}
```

`role` is `editor`, `viewer` or `none`. A `none` row for an owner or admin of
the organization answers `422`: they always see every project. Deleting a row
returns its user to the organization's default access.

Adding someone who is already a member answers `409`; an unknown user or
membership answers `404`. When you add an event-type owner
(`POST /api/v1/projects/{slug}/event-types/{event_type_id}/owners`) or a branch
reviewer (`POST /api/v1/projects/{slug}/branches/{branch_id}/reviewers`), the
user must have access to the project, or the call answers `422`
`User is not a member of this project`. Removing a member also removes their
event-type ownerships and pending branch-reviewer assignments in that project,
and closes a live-updates stream they have open within one heartbeat.

One existence signal is unavoidable: slugs are unique within an organization,
so `POST /api/v1/projects` or a rename to a slug that is already taken in the
same organization answers `409` even when the caller cannot see the project
holding it. Another organization may use the same slug. Reserved slugs (`demo`,
`orgs`, `new`, `settings`, `api`, `p`, `o`, ...) answer `422`.

### Creating a project from a template {#project-templates}

List the built-in [project templates](../use/project-templates.md) (any
authenticated caller; a project-bound key gets `403` like on every
instance-wide route):

```http
GET /api/v1/project-templates
```

Each item carries `id` (`ecommerce`, `subscriptions`, `mobile_games`,
`b2b_saas`), `version`, `name`, `description`, `branch_name` (for example
`template/ecommerce`), `counts` (`event_types`, `fields`, `events`,
`variables`, `metric_suggestions`, `alert_suggestions`), `event_type_names`,
and the informational `metric_suggestions` and `alert_suggestions`.
Suggestions carry no id: nothing is ever created from them.

A metric suggestion has `name`, `display_name`, `description`, `needs`
(`scan` / `data_source`) and the metric-definition `kind` to create:

| `kind` | `composition` | `numerator_event` / `denominator_event` | `needs` |
| --- | --- | --- | --- |
| `event_composition` | `single` (a count) or `ratio` | The plan event names; `denominator_event` only for `ratio` | `scan` |
| `fact` | `null` | `null` (reads a fact table) | `data_source` |
| `sql` | `null` | `null` (reads a SQL query) | `data_source` |

An alert suggestion has `name`, `description` and `needs`
(`alert_destination`).

Create the project with the template's id (editor role, `write` key without
`project_slug`):

```http
POST /api/v1/projects
{"name": "Shop", "slug": "shop", "template_id": "ecommerce"}
```

The `201` response is the usual project plus `template_branch_id`, the draft
working branch holding the template's event types, fields, properties and draft
events. Pass it as `?branch=` to review or edit the plan, then submit, approve
and merge it through the ordinary branch flow; main stays empty until then, so
the project's summary counters read `0`. Without `template_id` the request is
unchanged and `template_branch_id` is `null`.

- An unknown `template_id` answers `422` `Unknown project template` before
  anything is written; an empty string is a schema `422`.
- The project, its main branch, the creator's membership and the template
  branch are one transaction: a failure while seeding leaves no project behind.
- The audit log gets `project.create` (its payload includes `template_id`) and
  `plan_branch.create` for the template branch, with `name` and `template_id`
  in the payload. The creator is the branch's author and is subscribed to it.
- No metric definition, fact table, data source, scan config, alert
  destination or alert rule is created; the branch description lists the
  suggestions as a checklist instead.

## Project And Branch Context

Most agent calls require a project slug in the path:

```text
/api/v1/projects/{slug}/...
```

Plan branch context is passed as the query parameter named `branch`:

```text
?branch=<branch_id>
```

If `branch` is omitted, services resolve the project's main branch. For read-only context gathering, omitting `branch` is usually correct. For proposed edits, pass the working branch id explicitly so the agent does not mutate the live plan by accident. Passing the main branch's own id is the same as omitting `branch`.

A merged branch is read-only, and a closed one is read-only until it is reopened: a write naming either answers `409` (`Branch '<name>' is merged, so its plan is read-only` or `Branch '<name>' is closed; reopen it before editing its plan`). Reads still work on both, and so does `POST /api/v1/projects/{slug}/search/reindex`; the AI `describe-event` and `describe-event-type` suggestions, which write nothing, are refused like writes. The `409` only reaches a caller the route would let write: a viewer or a `read`-scope key gets the route's own `403` first. On a branch-scoped write, `401` comes first, the route's `403` before the `409`, and the `409` before the route's own `404`s and body-schema `422`s; whether a malformed or foreign `branch` (`400` / `404`) or the `403` answers first depends on the route. A body sent with a JSON `Content-Type` (`application/json` or `application/*+json`) that is not valid JSON is a `422` before any of these, authentication included; under any other `Content-Type`, or none, the body is only checked against the route's schema after them.

The photo and Figma spec writes (`POST /api/v1/projects/{slug}/events/{event_id}/photos`, `POST .../photos/figma`, `PATCH .../photos/reorder` and `DELETE .../photos/{photo_id}`) take no `branch`: they address a branch's event by its own id. They answer the same `409` when that event belongs to a merged or closed branch. Their order differs, and two of the refusals come before authentication rather than after it: the whole router caps the request body, so an upload declaring a `Content-Length` over `PHOTO_MAX_SIZE_MB` — or streaming past it — is `413` before any dependency runs, `401` included, and a malformed JSON body on `photos/figma` or `photos/reorder` is `422` in the same place. Everything after that is dependency-ordered: `401`, then the route's `403`, then the `404` for an unknown event, then the `409`, and only then the file's own `415` / `422`, the `404` for an unknown photo or the `400` for an incomplete reorder list. Comments, on a photo or on the event, are not plan content and are accepted on any branch.

Plan writes and merges never interleave. A write to a branch that is being merged (a `?branch=` write, a revert, or a photo or Figma spec write) waits until the merge commits and then answers the `409` above; a merge that starts while a write to its branch is in flight waits for it, so a write after the approval makes the merge answer `409` `insufficient_approvals` with the approval counted as stale. A write to main waits for any merge in progress and then applies on top of the merged plan, and a merge that starts during a write to main waits for it and reports a `409` `conflicts` where the two disagree, rather than overwriting it. A comment on a branch's event posted during that branch's merge waits as well, then lands in the thread on main. The wait lasts as long as the merge takes, which grows with the size of the plan; the AI `describe-event` and `describe-event-type` suggestions, which write nothing, never wait.

Passing `branch` also makes the write **attributable**: the audit log records the entry against that working branch, by id and by name, and an owner reading the log sees a branch chip on the row. A write with no `branch` carries no chip, which covers both a deliberate write to main and an action that has no branch dimension at all — and passing the **main** branch's own id records no branch either, by design, so one write to main cannot render two ways. So an agent's branch-scoped edits are distinguishable after the fact from writes to the live plan — which is the other reason to pass the id rather than rely on the default. This applies to the branch-scoped plan writes (`event.*`, `field.*`, `event_type.*`, `variable.*`, `meta_field.*`, `relation.*`, and `project.retire_unused_variables`). Drift resolutions (`variable.drift_action`, `schema_drift.*`) are the exception: a drift is only ever detected against main, so accepting one is always a write to the main plan and carries no chip whatever `branch` you pass. Event writes are recorded as `event.create`, `event.bulk_create`, `event.update`, `event.bulk_update`, `event.delete` and `event.bulk_delete`; a bulk route files one row per request, with the ids (and, for a delete, the names) in the payload. Reordering an event is not recorded, and neither are events written by a scan — but accepting a scan's shadow-event candidate is a plan write, not a scan write, and files `event.create` like any other, with the candidate it was admitted from named in the payload; dismissing one files `shadow_event.dismiss` against the candidate and carries no branch, a candidate having no branch to name. Events additionally keep their own per-event history (`GET /projects/{slug}/events/{event_id}/history`): a `created` row first, then before/after rows keyed `status`, `name`, `title`, `description`, `sunset_at`, `tags`, `field:<field name>` and `meta:<meta field name>`. That history is removed with the event, while the audit row is not — so a deleted event's `field_values` are recoverable from neither surface.

Discover branches:

```http
GET /api/v1/projects/{slug}/branches
```

The response includes each branch `id`, `name`, `kind`, and `status`. Use the `id` as the `branch` query parameter on plan endpoints.

Add `?include_diff_counts=true` when you need a per-branch summary rather than the branches themselves. Each open working branch (`draft`, `ready_for_review`, `changes_requested` or `approved`) then also carries `ahead` (how many entities it changed against its base, counting a rename as one change, as the branch's diff view does) and `behind_base` (whether main moved since the branch was cut), computed for the whole list from a single main snapshot — one request instead of a `/diff` call per branch. Merged and closed branches keep both `null`, like main. It is opt-in because building those snapshots is the expensive part of the response; leave it off when you only need the branch rows.

Create a branch with `POST /api/v1/projects/{slug}/branches` (editor role). It answers `409` in two cases that only `detail` tells apart: `Branch with this name already exists`, and `The project changed while the branch was being created. Please try again.` The second is rare: on Postgres the server already retries a creation the database aborts as unserializable (in practice, a project rename landing in the same instant), up to three attempts, and answers `409` only when all three are aborted. Nothing of a failed attempt is kept, so send the same request again.

Review what a working branch changed, and undo one change of it:

```http
GET  /api/v1/projects/{slug}/branches/{branch_id}/diff
POST /api/v1/projects/{slug}/branches/{branch_id}/revert
```

The diff returns one entry per changed entity, each carrying `entity_type`, `kind` (`added` / `changed` / `removed`), `name`, `parent`, the `entity_id` it describes, and — for a changed entity — `field_changes`. A collection-valued field there additionally breaks down into `items`, keyed by the member that moved (a field name, a tag, the event an override targets).

Names are not always unique: two events can share a type and name, and two relations can link the same two fields. Each branch copy records the `main` row it was made from, so the diff, the merge and a revert pair such rows one by one: each gets its own entry, and an entry's `entity_id` (the branch row, or the base row for a removal) tells them apart. Only on a branch opened before copies recorded their origin can a name still stand for several rows the server cannot tell apart; an entry for such a name carries a warning in `warnings` telling you to rename one of the events, or remove one of the relations, before changing either.

Read the response's `renames` list before interpreting those entries. Entities are keyed by name, so a rename arrives split in two — a removal of the old name beside an addition of the new one — which reads as a deletion your agent never made. Each `renames` element (`entity_type`, `parent`, `removed_name`, `added_name`) names the two entries the merge will treat as **one** renamed row, keeping the entity's id and everything hanging off it. The pairing is stated by the server because it also depends on `main`, which the diff you are holding does not show.

`revert` takes the coordinates of one such entry and restores it to the branch's base state, responding with the resulting diff:

```json
{ "entity_type": "event", "name": "purchase:success", "parent": "track", "field": "field_values", "entity_id": "5a1f…" }
```

Pass the entry's `entity_id` as well: when two entries share a name it is the only thing that says which one you mean, and without it such a name is refused with `409` (`More than one change on this branch is called …`). Omit `field` to revert the whole entity: an addition is deleted, an edit is written back, a deletion is rebuilt with its child rows and, for an event, its `superseded_by` successor. A revert never touches main, needs an open branch and an editor role, and answers with a `409` — rather than a partial write — when the change cannot be undone unambiguously: two entities on the branch answer to the name and nothing records which one the entry is about (`Rename one of them, then revert.`), several rows of the branch's base snapshot answer to it with none of them named by the entry or a copy's origin (`Undo it by hand instead.`), two base events share the name of an event a restored property override points at (`Set the overrides by hand instead.`), two events answer to the `superseded_by` successor being restored, on the branch or in the base, the parent event type is still deleted, or the branch's base snapshot predates a field the entity needs. A restored `superseded_by` whose successor no longer exists on the branch is cleared instead. A merged branch answers `409` `Branch is merged, so its plan is read-only`, and a closed one `Branch is closed — reopen it before reverting changes`.

### Updating a branch from main

When main changes after a branch is cut, the branch is *behind*: `GET /api/v1/projects/{slug}/branches/{branch_id}/conflicts` answers `behind: true`. Bring main's changes onto the branch with a three-way merge of main INTO the branch rather than recreating it:

1. `GET /api/v1/projects/{slug}/branches/{branch_id}/update-from-main` (any member, read-only) returns `behind`, `updatable`, `blockers`, `main_hash`, `main_changes` (per entity type: `added`, `changed`, `removed`, `renamed`) and `conflicts`: every field both sides changed since the base, for all six entity types, grouped per entity with `name` (the key a choice is stored under), `parent`, `label`, and per field `base`, `ours` (main), `theirs` (the branch), `choice` and `dependents`. A field of `@presence` means one side deleted what the other changed; its values are `"present"` / `"absent"`, and `dependents` counts the branch's own work under an event type that taking main's deletion would also remove.
2. `POST` the same path (editor) with `{"expected_main_hash": "<main_hash from the preview>", "resolutions": [{"entity_type", "entity_name", "field_name", "choice"}]}`. `choice` names the value to end with: `ours` takes main's, `theirs` keeps the branch's. Inline choices are stored in the same transaction. Choices saved earlier through `POST .../resolutions` count only when `expected_main_hash` is sent, because a stored choice records a side, not the values it was made against.

On success the answer is `200` with `updated`, the branch, `applied` counts and the old and new `base_revision_id`: the branch's base is now main, so its diff shows only its own work and the next merge has nothing to refuse. A branch already level with main answers `200` with `updated: false` and writes nothing. Every refusal is a `409` that writes nothing:

| `detail` | Meaning |
|---|---|
| `unresolved_conflicts` (with `conflicts`) | Some overlapping field has no choice yet. Resolve them and post again. |
| `update_blocked` | One of the preview's `blockers`: `ambiguous` (main changed a row the branch holds twice under one name, on a branch cut before origin tracking; copy your changes to a new branch) or `identity_clash` (a row of main's and one of the branch's own would share a name or `source_name`; rename the branch's one). |
| `main_moved` | Main changed after the preview that produced `expected_main_hash`. Preview again and review the new changes. |
| `incomplete_base_snapshot` | The branch predates complete merge baselines and cannot be updated. `updatable` is already `false` in the preview and in `/conflicts`. Copy your changes to a new branch. |
| `update_constraint_violation` | The database refused a uniqueness rule the preview could not foresee. |
| plain string | The branch is merged or closed. |

A successful update is audited as `plan_branch.update_from_main`.

## Search And Retrieval Flow

Start with project search when the agent has a natural-language question or a partial event name:

```http
GET /api/v1/projects/{slug}/search?q=purchase%20success&types=event&limit=10
GET /api/v1/projects/{slug}/search?q=user_id&types=variable&limit=10&branch=<branch_id>
```

Useful query parameters:

- `q`: required search text, 1 to 500 characters.
- `types`: optional repeated filter, taking the same values a result's
  `entity_type` carries. The accepted set is enumerated on the parameter itself
  in `/openapi.json` — read it from there rather than from a list here, since it
  grows as new kinds are indexed. It spans plan content and project
  configuration alike, so scan configs and alert rules are filterable values,
  and so are docs catalog notes (`doc`, see [Docs catalog](#docs-catalog)).
- `include_archived`: defaults to `false`.
- `semantic`: defaults to `true`. `false` skips the embedding leg and answers
  from the keyword index alone — much sooner, with `semantic_used` always
  `false`. The command palette asks this way first and upgrades to the full
  answer when it lands.
- `limit`: 1 to 100, defaults to 20.
- `branch`: optional branch id.
- `group_variants`: defaults to `false`. `true` folds events of one event type
  whose names differ only in one naming-rule placeholder into their best-ranked
  hit, which then carries a `variant_group`
  (`key`, `pattern`, `placeholder`, `count`, `variants[]`); the other members
  are not returned as separate results. `limit`, `total` and `truncated` then
  count rows, so a folded group is one.

Search results include `entity_type`, `entity_id`, `title`, `subtitle`,
`description`, `snippet`, `route_path`, `score`, `confidence`, and `highlights`.
Results linked to a concrete catalog event also include `event_id`, `name`, the
compatibility `implemented` projection, and safe `variable_values` contexts with
possible values for non-sensitive fields.

Use entity-specific endpoints for full context after search:

```http
GET /api/v1/projects/{slug}/events/{event_id}?branch=<branch_id>
GET /api/v1/projects/{slug}/events?search=purchase&limit=50&branch=<branch_id>
GET /api/v1/projects/{slug}/event-types
GET /api/v1/projects/{slug}/event-types/{event_type_id}
GET /api/v1/projects/{slug}/event-types/{event_type_id}/fields
GET /api/v1/projects/{slug}/properties?limit=200&offset=0&branch=<branch_id>
GET /api/v1/projects/{slug}/properties/{variable_id}/values?branch=<branch_id>
GET /api/v1/projects/{slug}/properties/{variable_id}/event-overrides?branch=<branch_id>
GET /api/v1/projects/{slug}/properties/drifts?branch=<branch_id>
```

`GET /projects/{slug}/events/{event_id}` and its `/history` answer for an event
on **any** branch of the project, whatever `branch` you pass or omit — a link
handed over with a branch id resolves without first looking the branch up — and
the response's `branch_id` says which branch the row belongs to. Writes stay
strict: a `PATCH` must name the event's own branch.

Event responses include:

- event identity and state: `name`, the free-text `title`, `description`,
  lifecycle `status`, `reviewed`, `owner_id`, optional `sunset_at`,
  `superseded_by_event_id`, `first_seen_at` (when a scan first saw the event
  with volume; `null` if never), and `branch_id`;
- `lifecycle_findings`: the event's sunset-watch findings (see
  [Event lifecycle](#event-lifecycle));
- event type id and brief event type data;
- field values and meta values;
- tags;
- metric breakdown columns;
- property value contexts on field values that contain real `${variable}` placeholders.

`/variables` is paginated and returns `{"items": [...], "total": <int>}`.
`offset` defaults to `0` (minimum `0`) and `limit` defaults to `200` (`1` to
`5000`); out-of-range or non-numeric values are rejected with `422`. Read `total`
to decide whether another page is needed rather than assuming one response holds
the whole catalog.

`usage=all|used|unused` narrows the listing: `unused` returns exactly the rows a
retirement pass would take, `used` its complement. It is answered by the same
retirement predicate rather than by a "zero usage count" shortcut, so `unused`
never offers up a property that a live event value still names. The default is
`all` and an unrecognised value is a `422`. `total` reflects the filter, so it
stays the honest count for whichever set you asked for.

Each item in `items` includes `allowed_values`, warehouse/JSON-path `bindings`,
`excluded_from_scans`, usage summaries, `open_drift_count`, and two inline
previews that spare a per-variable follow-up call: `sample_values` (observed
values unioned across every context, de-duplicated, capped at 20) and
`event_names` (distinct names of the events the property was observed in,
alphabetical, capped at 20 — `event_count` carries the untruncated total).

`/variables/{variable_id}/values` returns the full per-event observed contexts
for one property: low-cardinality contexts list all observed values, while
high-cardinality contexts list bounded samples and an observed count. A context
over a plain column takes its kind and its count from a `COUNT(DISTINCT)` over
the scanned window, but one over a JSON-path binding is always high-cardinality
and counts only what a capped sample turned up — report "at least N", never N.
Reach for it only when the inline previews are not enough. Event overrides
replace the global documented list for their event.

The catalog is not append-only. A catalog scan run can retire the scan-created
properties nothing refers to any more — no `${token}` in any stored event field
or meta value, no observed context, no value drift, no per-event override — so a
property id cached from an earlier read can be gone by the next call. A scan
started by hand always retires; a scheduled collection retires too, judging a
property minted from a path inside a JSON column on every run and one minted
from a scalar column only when the config declares a lookback window, because
one quiet interval can flip a scalar column to literals in every event at once
and a run must not recycle the property on that evidence; a replay never. A
property your agent edited, documented, bound, or excluded from scans is never
retired, and so is one renamed to anything the scan would not have chosen for
that path itself.
The branch-wide version of the same pass,
`POST /projects/{slug}/danger/retire-unused-variables`, is not available to
agents: it takes the strict owner gate and rejects every API key.

## Updating Events

Agents that only read should use a `read` key. Agents that edit need a `write` key backed by an editor or owner user.

Patch one event:

```http
PATCH /api/v1/projects/{slug}/events/{event_id}?branch=<branch_id>
Content-Type: application/json
Authorization: Bearer tk_...
```

Example payload for a description-only update:

```json
{
  "description": "Fired after checkout succeeds and the order id is available."
}
```

Example payload for state-only review workflow:

```json
{
  "reviewed": false,
  "status": "in_review"
}
```

`EventUpdate` fields are optional and partial:

- `name`
- `title` — a free-text label (max 500), shown beside the name and searchable,
  never part of the scan identity; `EventCreate` takes the same field,
  defaulting to `""`
- `description`
- `status`
- `sunset_at`
- `owner_id`
- `reviewed`
- `metric_breakdown_columns`
- `tags`
- `field_values`
- `meta_values`

When updating `field_values` or `meta_values`, send the full replacement list
for that collection. For narrow text edits, prefer patching only `description`,
`title`, `name`, tags, or state fields — and where a scan names the type, fix a
wrong label through `title`, since `name` is the identity. Values written
through event mutations are treated as authored and are protected from later
scan overwrite; re-sending an unchanged value keeps its flag as it was.

On every partial-update body in the API — events, event types, fields, meta
fields, scan configs, data sources, properties and projects — omitting a field is
how you leave it alone, and sending it as an explicit `null` means "clear it".
A `null` on a field whose column cannot be empty is refused with a `422` naming
the field (`Field(s) cannot be null: status`). On `EventUpdate` those are `name`,
`description`, `status` and `reviewed`; `sunset_at`, `owner_id` and
`superseded_by_event_id` all accept a `null` and clear, `title` reads a `null` as
`""`, `metric_breakdown_columns` reads one as `[]`, and `tags`, `field_values`
and `meta_values` read one as "leave the children alone". These requests all
failed before; only the status code and the message changed.

Every `meta_field_definition_id` in a patch, and the `event_type_id` in a create,
must come from a listing read with the same `branch` you are writing to. A branch
holds its own copy of every event type and meta field under a new id, so an id
read without `branch` is `main`'s and is refused with a `422` on a branch write.
Because `meta_values` is a full-list replacement, you cannot get past that `422`
by dropping the offending entry without losing the event's other meta values —
re-read the meta fields on the right branch instead. Tags are
stored lower-cased, trimmed and de-duplicated, and one over 100 characters is a
`422`; a meta value over 2,000 bytes as stored is a `422` too (for a field with
a link template, only the part the template wraps is stored).

Event create and patch return `EventMutationResponse`, which is the event plus a
`warnings` array. When a scan config governs the event type with an
`event_name_format`, manual creation derives the canonical name from the
referenced field values. Missing template values produce `422`; a derived name
another event of the type already holds produces `409` naming that event — the
scan identity is a unique key in the database, so two creates racing for one
name end the same way, the loser with that `409` and never a second event, and
`POST /projects/{slug}/events/bulk` prefixes the same message with
`Event N of M: `; a differing client-supplied name is ignored with a warning.
Read the mutation response and use its returned name/id instead of assuming
your proposed name became the identity. The resolved rule is on the event type
itself — `event_name_format` on `GET /event-types` and
`GET /event-types/{event_type_id}`, `null` when no scan names the type — and it
governs a branch copy of the type exactly as it governs `main`, so read it there
rather than re-deriving it from the scan config list.

Bulk state updates are available for review/archive workflows:

```http
POST /api/v1/projects/{slug}/events/bulk-update?branch=<branch_id>
```

Payload:

```json
{
  "event_ids": ["00000000-0000-0000-0000-000000000000"],
  "reviewed": true,
  "status": "ready_for_dev"
}
```

The uniform bulk patch supports `status`, `sunset_at`, `owner_id`, and
`reviewed`. Bulk delete is a separate endpoint; both are write operations.

Which fields you **send** is what the request means, not what values they hold.
A field you leave out is left alone across the whole selection. An explicit
`null` for `sunset_at` or `owner_id` clears that field across the whole
selection — `{"event_ids": [...], "owner_id": null}` is how you unassign a
selection, and it is the only way to do it. `status` and `reviewed` are NOT NULL
columns: an explicit `null` for either is refused with 422. A body that sends
nothing but `event_ids` is refused with 422 as well. The web UI spells the same
unassign as an **Unassign** entry in the bulk bar's owner picker.

## Search Indexing

The API reindexes the affected branch after normal plan mutations. Agents usually do not need to call reindex manually.

Manual reindex is editor-only:

```http
POST /api/v1/projects/{slug}/search/reindex?branch=<branch_id>
```

Use this after out-of-band maintenance or imports if search results look stale. When embeddings are enabled, the normal embedding refresh flow is scheduled by the backend.

## Dry-Running a Scan

Ask what a scan config *would* create, without writing anything:

```http
POST /api/v1/projects/{slug}/scans/dry-run
```

Send either a saved config:

```json
{ "scan_config_id": "…", "sample_row_limit": 5000 }
```

or a draft, in which case `data_source_id` and `base_query` are both required and
every other field is optional (`event_type_id`, `event_type_column`,
`time_column`, `event_name_format`, `event_group_rules`, `json_value_paths`,
`cardinality_threshold`, `app_version_column`, `platform_column`,
`scan_lookback_hours`). When `scan_config_id` is present the draft fields are
ignored.

It answers `202` with a job record; poll it:

```http
GET /api/v1/projects/{slug}/scans/dry-run-jobs/{job_id}
```

Same 202-and-poll shape as `/scans/preview`, and for the same reason: a dry run
issues the same `GROUP BY ALL` a real scan issues, which can outlive a gateway
timeout. While `status` is `pending` or `running`, `result_summary` is `null`.
On `completed` it holds:

```json
{
  "window_from": "2026-08-07T12:00:00Z",
  "window_to": "2026-08-08T12:00:00Z",
  "sampled_rows": 4812,
  "sample_row_limit": 5000,
  "sample_is_complete": false,
  "breakdown_combinations": 143,
  "events": [
    {
      "name": "Purchase Completed",
      "source_name": "Purchase Completed",
      "event_type": "Purchase",
      "approx_row_count": 3120,
      "share_of_sample": 0.648,
      "status": "new",
      "grouped_by_rule": null,
      "count_confidence": "sampled"
    }
  ],
  "events_truncated": true,
  "max_events_reached": false,
  "fields": [{ "name": "props", "type": "json", "status": "new", "event_type": "Purchase" }],
  "templated_columns": [{ "column": "country", "distinct_values": 214, "threshold": 100 }],
  "reserved_columns": ["ts", "app_version"],
  "unmapped_columns": ["legacy_flag"],
  "warnings": [],
  "errors": []
}
```

An event is identified by `event_type` **and** `source_name`, never by the name
alone: a run writes one event per event type, so a grouped scan
(`event_type_column`) whose name format collapses to the same string under two
event types produces two entries here — and `status` is resolved against that
event type's plan, not against a union.

Read it honestly. `sample_is_complete: false` means more distinct events exist
than the pass examined, so report "at least N", never N. `count_confidence` is
`"exact"` only when the sample is complete *and* no lookback window applied.
`share_of_sample` is deliberately offered instead of a projected table-wide
total — do not compute one. `errors` carries event-name-format failures verbatim
and does **not** fail the job; a non-empty `errors` means the config would fail
every real run.

The response also carries `name_warnings`, from the
[duplicate check](#duplicates-and-naming) over the events the run would add.
It is scored lexically only:

```json
"name_warnings": [
  {
    "code": "combinatorial_explosion",
    "event_type": "Promo",
    "message": "80 new names under 'Promo' differ only in {offer} (…)",
    "count": 80,
    "slot": 1,
    "slot_label": "{offer}",
    "pattern": "promo_banner_click:*",
    "samples": ["promo_banner_click:offer_001", "promo_banner_click:offer_002"]
  },
  {
    "code": "duplicate",
    "event_type": "Purchase",
    "name": "Purchase Complete",
    "message": "New event 'Purchase Complete' looks like 'Purchase Completed' (…)",
    "duplicate_of": { "event_id": "5a1f…", "name": "Purchase Completed",
                      "score": 0.9189, "status": "live" }
  }
]
```

- `combinatorial_explosion` is raised only when the scan has a name rule: more
  than 50 new names under one event type are the same except in one slot, and
  that slot's distinct values (`count`) are still at or below the scan's
  `cardinality_threshold`. Above the threshold the column already becomes a
  `${...}` template. `pattern` shows the fixed slots with `*`; `samples` holds
  up to five names.
- `duplicate` is raised for a new name that looks like an event of the same
  event type already on main (`duplicate_of`, the best match). At most 200 new
  names are checked per run.

Both are best-effort: when the check cannot run the list is empty, and the job
still completes.

Both routes are **owner-only** and session-only (an API key cannot reach them),
because the draft's `base_query` is free-text SQL run against a stored warehouse
credential. This is the same gate `/scans/preview` carries.

## Replaying Metrics

Recollect an existing scan config's metrics over a window you name:

```http
POST /api/v1/projects/{slug}/scans/{scan_id}/metrics/replay
```

```json
{
  "time_from": "2026-04-01T00:00:00Z",
  "time_to": "2026-04-02T00:00:00Z"
}
```

It answers `201` with the queued `ScanJob`; poll
`GET /api/v1/projects/{slug}/scans/{scan_id}/jobs` for its status. Use it to
backfill a window the scheduler missed or to recompute after a metric definition
changed. The config must already carry `time_column` and `interval`, otherwise
the call is `400`.

`time_to` must land **at or before the last completed interval**. The interval
still filling holds no complete bucket to replay, so a period reaching into it is
now a `400` — *"Replay period must end at or before … UTC"*, naming the latest
end it would accept — where it previously answered `201` and then produced a
failed run. An agent that posts a window ending at "now" must floor that end to
the config's own interval first.

This is the **only** owner-gated route an API key can reach, and the gate is
strict about all three of its parts: the key's scope must be `write`, the user
behind it must be an owner or admin of the organization the key belongs to (and
so of the project's), and a project-bound key still only reaches its own
project. A member's `write` key gets `403 Organization owner or admin role
required`; a `read` key gets `403 API key has read-only scope`.

It is reachable because a replay only re-runs SQL an owner or admin already
authored through the browser-only scan routes — it cannot introduce a new query.
Creating or editing a scan config, like connecting a data source, stays an
interactive session of an owner or admin.

## Source freshness

Before reading a drop as real, check whether the source behind it is simply
late. Freshness for every scan config in a project is one call:

```http
GET /api/v1/projects/{slug}/source-freshness
```

It returns one item per scan config, with `id`, `name`, `data_source_id` and
`freshness`. The same `freshness` object is on every `ScanConfig` response
(`GET /api/v1/projects/{slug}/scans` and a single scan), so an agent that
already holds a scan config does not need the second call.

| `freshness` field | Meaning |
|-------|---------|
| `status` | `fresh`, `late`, `overdue` or `unknown`. |
| `lag_seconds` | Seconds between now and `last_event_at`. `null` when no event has been observed. |
| `last_event_at` | The start of the newest bucket that had events in the latest successful metrics collection. It has bucket resolution, so it can sit up to one interval behind the newest event. `null` before the first collection. |
| `last_collection_at` | When that collection completed. `null` before the first collection. |
| `expected_by` | The moment after which the source counts as `late`. `null` when the status is `unknown`. |

The status is computed each time the response is built, and is never stored:

- `overdue`: `now − last_collection_at > 2 × interval`. The scan is not
  running on schedule. This takes precedence over `late`.
- `late`: `now − last_event_at` reaches
  `min(3 × interval, settling allowance rounded up to whole intervals + 2 × interval)`,
  where the settling allowance is the project's
  `anomaly_ingestion_settling_minutes`.
- `unknown`: the scan has no interval (a manual scan), or nothing has been
  collected yet.
- `fresh`: otherwise.

While a scan is `late` or `overdue`, the metrics worker holds that scan's
drop-direction volume signals instead of emitting them. A missing drop on a
late source therefore means "not yet judged", not "no drop". The scan job's
`result_summary` reports this as `freshness_status` and `signals_held`. Once
data lands, the next run re-collects the buckets that were empty during the
delay before scoring them. A replay only refreshes `last_event_at` when it
reaches past it. See
[Drop signals are held while a source is late](../use/anomaly-detection.md#held-while-late)
and the [Source freshness](../use/alerting.md#source-freshness) alert scope,
which rules enable with `include_source_freshness`. A `late` alert comes from
the scan's own collection run; an `overdue` alert comes from a sweep that runs
every 15 minutes. One delay or outage is one alert.

## Chart annotations

Annotations are the markers charts draw at a point in time. A deploy pipeline
posts one per release so the next anomaly on a chart sits next to the deploy
that probably caused it:

```http
POST /api/v1/projects/{slug}/annotations
```

```json
{
  "label": "Deployed web 2026.09.25",
  "bucket": "2026-09-25T14:02:00Z",
  "source": "api",
  "url": "https://github.com/acme/web/releases/tag/2026.09.25"
}
```

| Field | Meaning |
|-------|---------|
| `label` | Required, 1–200 characters. |
| `bucket` | Required. When it happened, as an ISO 8601 timestamp. |
| `source` | `manual` (the default, what the app's own form sends) or `api`. `release` is reserved for the markers the metrics worker draws itself and answers `422` from a client. |
| `url` | Optional link the chart tooltip opens: `http` or `https` only, at most 500 characters. |
| `description` | Optional, up to 2000 characters. |
| `color` | Optional. Pipeline and release markers draw muted whatever colour they carry. |
| `scope_type`, `scope_ref` | Optional, and both or neither: `project_total`, `event_type`, `event` or `metric`, plus the id it names. Omit both for a project-level marker, which every monitoring (Volume tab) chart in the project shows. |

The response is the annotation, with `source` and `url` echoed back.

**Authentication.** The route is editor-level: a `write` API key backed by an
editor or owner, like every other mutation. Bind the key to the project with
`project_slug` so a leaked CI secret can annotate one project and nothing else.

**De-duplication.** Only `source: "api"` is de-duplicated on this route: the
same `(project, label)` posted with source `api` within the last 24 hours is not
created again, and the API answers **`200`** with the existing
annotation instead of **`201`** with a new one. A retried deploy job therefore
draws one marker, not two — so check the status code, not just the body, if you
need to know which happened. Put the version or commit in the label when two
deploys in a day are two separate events. `release` markers are unique per
label per project for good: one **Release *version*** marker, ever. `manual`
annotations (the default `source`) are **never** de-duplicated — every manual
create answers `201` with a new row, so a CI job that wants retry-safety must
send `"source": "api"`.

The same request from the command line is
[`tripl annotate`](../run/cli.md#tripl-annotate), which prints which of the
two answers it got. A GitHub Actions deploy step, with `curl`:

```yaml
- name: Mark the deploy on tripl charts
  run: |
    curl -fsS -X POST "https://tripl.example.com/api/v1/projects/prod/annotations" \
      -H "Authorization: Bearer ${{ secrets.TRIPL_WRITE_KEY }}" \
      -H "Content-Type: application/json" \
      -d "{\"label\": \"Deployed web ${{ github.ref_name }}\", \"bucket\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"source\": \"api\", \"url\": \"${{ github.server_url }}/${{ github.repository }}/releases/tag/${{ github.ref_name }}\"}"
```

`-f` fails the step on a `4xx`/`5xx`; a `200` for a de-duplicated label is a
success. How charts draw these markers, and the automatic **Release *version***
markers beside them, is in
[Chart annotations](../use/feature-reference.md#chart-annotations).

## Signal verdicts {#signal-verdicts}

A verdict records what a monitoring signal turned out to be. Set one with an
editor-level `write` key:

```http
POST /api/v1/projects/{slug}/signals/verdict
```

```json
{
  "scope_type": "event",
  "scope_ref": "d4c684dd-…",
  "scan_config_id": "5b1e…",
  "bucket": "2026-09-25T18:00:00Z",
  "verdict": "tracking_bug",
  "note": "Fires twice on Android 7.4.0"
}
```

| Field | Meaning |
|-------|---------|
| `scope_type`, `scope_ref`, `scan_config_id`, `bucket` | The signal's key, as on the signal itself. `scan_config_id` is `null` for a catalog metric. |
| `verdict` | `expected`, `tracking_bug`, `false_positive` or `real_issue`. |
| `expected_reason` | With `expected` only: `campaign`, `release`, `seasonality` or `other`. |
| `note` | Optional free text. The author and time are recorded from the request. |

`DELETE` on the same path with the same key clears the verdict. What each one
does: `expected` writes an annotation on the bucket and hides that one signal
(it does not suppress later buckets); `false_positive` tunes the scope's
detection thresholds exactly like an incident false positive. When the signal
belongs to an incident, the request updates the incident instead —
`false_positive` → `false_positive`, `real_issue` and `tracking_bug` →
`acknowledged`, `expected` → `resolved`. See
[Verdicts](../use/anomaly-detection.md#signal-verdicts).

Signal payloads carry the result as `verdict` —
`{verdict, expected_reason, note, author_name, created_at, source}`, where
`source` is `signal` or `incident` — and `incident` — `{id, status}` — each
`null` when absent. To list only undecided signals, add `needs_verdict=true` to
`GET /api/v1/projects/{slug}/anomalies/signals`.

Per-project totals, for example for a health score, are one read-level call:

```http
GET /api/v1/projects/{slug}/signals/verdict-counts
```

```json
{
  "needs_verdict": 4,
  "expected": 2,
  "tracking_bug": 1,
  "false_positive": 3,
  "real_issue": 0
}
```

`needs_verdict` counts the signals without a verdict; the other four count
signals by the verdict they carry. An acknowledged signal has no verdict and
counts under `needs_verdict`.

## Owner notifications {#owner-notifications}

A rule can email the owners of what it matched, in addition to its destination.
The switch is `notify_owners` (boolean, default `false`) on the alert rule —
accepted by create
(`POST /api/v1/projects/{slug}/alert-destinations/{destination_id}/rules`) and
update (`PATCH …/rules/{rule_id}`), and returned on every rule response.

Owners of a matched item are the event type's owners on `main` (for an event,
its type's owners; for an event type, its own; for any other scope about an
event or event type — drift, release regression, lifecycle — that type's
owners) plus, for a catalog metric, the metric's `owner_id`; project total and
source freshness have none. Only owners who can currently see the project (a
row, the organization's default access, or an owner or admin of the
organization) and have an account email are emailed; an owner without access
or without an email is neither notified nor listed. Each owner gets one plain-text email per rule delivery,
sent after the rule's delivery is sent, through the instance SMTP settings. The
email uses the default item lines (the digest's lines for a digest), not the
rule's custom template. A digest that batches several rules sends one email
per rule delivery, so an owner of items in two of those rules can get two
emails.

The delivery detail (`GET /api/v1/projects/{slug}/alert-deliveries/{delivery_id}`)
carries the result as `owner_notifications`; the list route does not include
it:

```json
[
  {"user_id": "8f0c…", "name": "Anna", "email": "anna@example.com", "status": "sent", "error": null, "sent_at": "2026-09-25T18:04:11Z"},
  {"user_id": "12ab…", "name": "Oleg", "email": "oleg@example.com", "status": "skipped", "error": "SMTP is not configured; owner email skipped.", "sent_at": null}
]
```

`status` is one of:

- `sent` — the email went out;
- `failed` — the mail server rejected it (see `error`); other owners are still
  attempted;
- `skipped` — email was unavailable: no SMTP server or Default From address,
  or a demo project;
- `pending` — a short-lived in-progress claim while the send runs; a claim
  older than 15 minutes is treated as abandoned and reclaimed.

The list is empty when the rule does not notify owners or nothing matched had
an owner. A `sent` owner is never emailed twice for the same delivery; a
retried or re-run delivery re-attempts `skipped` and `failed` owners (and stale
`pending` ones).

Two editor-level routes send a one-off email to the current owners:

```http
POST /api/v1/projects/{slug}/alert-inbox/{correlation_group_id}/notify-owners
POST /api/v1/projects/{slug}/signals/notify-owners
```

The first targets an incident. The second targets a signal — including one no
rule routed — and takes the signal key used by
[signal verdicts](#signal-verdicts):

```json
{
  "scope_type": "event_type",
  "scope_ref": "3a9e…",
  "scan_config_id": "5b1e…",
  "bucket": "2026-09-25T18:00:00Z"
}
```

Both return the owners they considered, with the same fields as above:

```json
{
  "owners": [
    {"user_id": "8f0c…", "name": "Anna", "email": "anna@example.com", "status": "sent", "error": null, "sent_at": "2026-09-27T09:12:40Z"},
    {"user_id": "12ab…", "name": "Oleg", "email": "oleg@example.com", "status": "skipped", "error": "notified 4 minutes ago", "sent_at": null}
  ]
}
```

A manual notify has a 10-minute cooldown per incident (or signal key) and
owner: an owner notified by hand within the last 10 minutes is returned as
`skipped` with an error such as `notified 4 minutes ago`. One request notifies
at most 20 owners.

Neither route depends on the rule's `notify_owners` setting. See
[Notifying owners](../use/alerting.md#owner-notifications).

## Incident summaries {#incident-summaries}

An incident (an alert-inbox correlation group) can have a short, cited
AI summary. The model and provider come from the instance AI settings. See
[The incident summary](../use/alerting.md#incident-summary) for what is
sent to the model. Values of sensitive fields and drift sample values are
never sent.

```http
GET  /api/v1/projects/{slug}/alert-inbox/{correlation_group_id}/summary
POST /api/v1/projects/{slug}/alert-inbox/{correlation_group_id}/summary
POST /api/v1/projects/{slug}/alert-inbox/{correlation_group_id}/summary/regenerate
```

- `GET` (any project member) returns the stored summary and whether it is
  current. It **never calls the model**.
- `POST .../summary` (any project member) returns the stored summary when its
  facts are unchanged. Otherwise it generates one, stores it and returns it.
  A `read`-scope API key gets `403` here: generating writes the stored
  summary and spends the model budget, so a read key can only `GET`.
- `POST .../summary/regenerate` (**editor**, not a `read` key) always generates.

No route takes a request body. An unknown group, or one with no items in this
project, is a `404`. AI being off and a failed generation are not errors: both
return `200` with the state in the body:

```json
{
  "correlation_group_id": "7c2d…",
  "state": "ready",
  "disabled_reason": null,
  "current_facts_hash": "9f1e…",
  "summary": {
    "sentences": [
      {"text": "Checkout Completed dropped 41% below expected.", "role": "what_broke", "fact_ids": [1, 3], "generated": true},
      {"text": "Most of the drop comes from platform = ios.", "role": "cause", "fact_ids": [4], "generated": true}
    ],
    "facts": [
      {"id": 1, "kind": "incident", "text": "Incident: a drop on Checkout Completed; …", "href": "/p/shop/alerting?incident=7c2d…"},
      {"id": 4, "kind": "attribution", "text": "Breakdown of Checkout Completed at 2026-09-27 08:00 UTC: 92% of the drop comes from platform = ios (…).", "href": "/p/shop/monitoring/event/1b7a…"}
    ],
    "cause_known": true,
    "facts_hash": "9f1e…",
    "generated_at": "2026-09-27T09:14:02Z"
  }
}
```

| `state` | Meaning | `summary` |
| --- | --- | --- |
| `disabled` | AI is off (`disabled_reason: "ai_off"`) or the project is a demo (`"demo"`). No facts are gathered, and `current_facts_hash` is `null`. | `null` |
| `missing` | Nothing has been generated yet (`GET` only). | `null` |
| `stale` | The facts changed since the stored summary was written (`GET` only). | the previous summary |
| `ready` | The summary matches the current facts. | the summary |
| `failed` | The provider returned nothing, or no sentence passed validation (`POST` only). Any stored summary is left as it was. Read it again with `GET`. | `null` |

To check whether a summary is current, compare `summary.facts_hash` with
`current_facts_hash`. `facts` are the facts **this** summary was written
from, so a stale summary's citations still resolve. `fact_ids` refer to
`facts[].id` (numbered from 1). `role` is one of `what_broke`, `cause`,
`release`, `history`, `discussion`, and `kind` is one of `incident`, `note`,
`scope`, `attribution`, `release`, `similar`, `comment`. `href` is an in-app
path, or `null`. A sentence with `generated: false` and empty `fact_ids`
is the fixed *The cause is unknown…* line that tripl adds when no cause
sentence was kept. In that case `cause_known` is `false`.

Calling `POST .../summary` on a `ready` summary is cheap, because it makes no
model call. `regenerate` makes a model call every time.

## Signal attribution {#signal-attribution}

Attribution says where a volume signal's change came from — which breakdown
column values explain the delta, and whether a release rolled out just before
it. It is computed when the anomaly is detected and stored with it, so every
surface returns the same numbers and the same sentences. See
[Why it changed: attribution](../use/anomaly-detection.md#attribution) for the
math.

Signal payloads — the signals list and a drilldown's `latest_signal` — carry
three fields. Chart anomaly points do **not** carry attribution; a chart point
that needs it goes through its signal, or through the per-anomaly route below.

| Field | Meaning |
|-------|---------|
| `anomaly_id` | The stored anomaly behind the signal — the id the per-anomaly route takes. |
| `attribution_status` | `ready`, `no_breakdown_columns` or `not_computed`. |
| `attribution` | The stored attribution when the status is `ready`, otherwise `null`. |

```json
{
  "attribution_status": "ready",
  "attribution": {
    "delta": -3390,
    "columns": [
      {
        "column": "platform",
        "explained_share": 0.92,
        "values": [
          {"value": "ios", "delta": -3120, "expected": 3400, "actual": 280, "share": 0.92},
          {"value": "web", "delta": 40, "expected": 900, "actual": 940, "share": -0.012}
        ]
      }
    ],
    "release": {
      "version": "4.12",
      "previous_version": "4.11",
      "share": 0.38,
      "reached_at": "2026-09-25T15:00:00Z"
    },
    "headline": "92% of the drop comes from platform = ios (−3,120 of −3,390)",
    "release_line": "Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop",
    "computed_at": "2026-09-25T18:04:11Z"
  }
}
```

| Field | Meaning |
|-------|---------|
| `delta` | The scope's actual minus expected in the flagged bucket. |
| `columns` | Up to 3 breakdown columns. `explained_share` (0–1) is the part of `delta` the column's same-direction top values explain. |
| `columns[].values` | Up to 3 named values per column: `expected`, `actual`, their difference `delta` (the value's contribution), and `share` — that contribution over the scope's `delta`, **signed**: a value that moved against the change has a negative share. The remainder outside the listed values is booked to *Other*, which is never listed as a value, so a column's contributions plus *Other* sum to `delta`. |
| `release` | `{version, previous_version, share, reached_at}` when a new app version crossed the release gate shortly before the bucket, else `null`. `previous_version` is `null` when no earlier released version carried the traffic. App-version series feed only this field, never `columns`. |
| `headline` | The one-sentence summary, or `null` when there is nothing to say. It is exactly the sentence the drilldown's **Why** panel and the [alert line](../use/alerting.md#attribution-line) print — quote it rather than rebuilding it from `columns`. |
| `release_line` | The release sentence, or `null` when `release` is `null`. Same rule: quote it verbatim. |
| `computed_at` | When the metrics worker stored this attribution. |

`headline` takes one of two forms:

- `<N>% of the <drop|spike> comes from <column> = <value> (<value delta> of <scope delta>)`
  — the column with the highest `explained_share`, and within it the largest
  value moving the same way as the delta. Counts use thousands separators and a
  real minus sign (`−`).
- `<Column> shifted in both directions; no single value explains the <drop|spike>`
  — with no percent, when that column's values moved in both directions by more
  than twice the delta in total, or none of them moved the same way as the
  delta.

`release_line` reads `Release <version> (after <previous>) reached <N>% of traffic <H>h before the <drop|spike>`,
with the hours rounded down, `at the <drop|spike>` in place of the lead time
when the release activated in the flagged bucket itself, and no `(after …)`
when `previous_version` is `null`.

To load it on its own, for example lazily on a detail view or for a chart point,
use a read-level key:

```http
GET /api/v1/projects/{slug}/anomalies/{anomaly_id}/attribution
```

```json
{
  "anomaly_id": "7c0e…",
  "scan_config_id": "2b9f…",
  "attribution_status": "ready",
  "attribution": {"delta": -3390, "columns": ["…same shape as above…"], "headline": "92% of the drop comes from platform = ios (−3,120 of −3,390)"}
}
```

It returns the anomaly's id, the scan it belongs to (`null` for catalog-metric
anomalies), and the same `attribution_status` and `attribution` a signal
carries. Attribution is read-only: replaying a period recomputes it, and it is
dropped with its anomaly.

## Dependencies and impact {#dependencies-and-impact}

Before an agent deletes, deprecates or renames a plan entity, it can ask what
depends on it. All three routes are read-only, need only a `read` key, and
follow the project's membership like every other `/projects/{slug}` route. None
of them changes what a delete, deprecate or rename does: those writes still
succeed with dependents in place. Fact tables keep their existing `409` when a
delete or edit would break a metric; nothing else gains one. See
[Dependencies & impact](../use/dependencies-and-impact.md) for which edges exist.

Look up one entity's dependencies:

```http
GET /api/v1/projects/{slug}/dependencies?entity=event:5a1f…&depth=1
```

`entity` is `<kind>:<id>`, with `kind` one of `event`, `event_type`, `field`,
`variable`, `metric`, `fact_table`, `alert_rule` or `relation`. `depth` is `1`
(the default, direct neighbours only) or `2` (one more hop, for example the
alert rules scoped to the metrics that use an event). Plan entities are resolved
on the branch named by the usual `branch` query parameter, or on main without
one; pass the branch copy's own id. An id that resolves to nothing is not an
error: the response is `200` with `entity.exists` set to `false`, `name` null,
and whatever project-wide rows still name that id.

```json
{
  "entity": { "kind": "event", "id": "5a1f…", "name": "checkout:completed",
              "exists": true },
  "upstream": [
    { "kind": "event_type", "id": "91c0…", "name": "checkout",
      "relation": "event belongs to event type", "certainty": "direct",
      "url_hint": "/p/shop/event-types/91c0…", "depth": 1 }
  ],
  "downstream": [
    { "kind": "metric", "id": "c3d2…", "name": "Checkout conversion",
      "relation": "metric uses event in its composition", "certainty": "direct",
      "url_hint": "/p/shop/monitoring/metric/c3d2…", "depth": 1 },
    { "kind": "alert_rule", "id": "0b7e…", "name": "Checkout volume",
      "relation": "alert rule filters on event", "certainty": "direct",
      "url_hint": "/p/shop/alerting", "depth": 1 },
    { "kind": "fact_table", "id": "e410…", "name": "orders",
      "relation": "fact table SQL or columns mention the column by name",
      "certainty": "possible",
      "url_hint": "/p/shop/metrics/fact-tables/e410…/edit", "depth": 1 }
  ],
  "counts_by_kind": { "metric": 1, "alert_rule": 1 },
  "possible_counts_by_kind": { "fact_table": 1 }
}
```

Every edge has the same shape: the other entity's `kind`, `id` and `name`, a
`relation` sentence saying why the edge exists, a `certainty`, a `url_hint` and
a `depth`. An edge's `kind` can also be `scan_config` (a scan's breakdown,
drift, platform or app-version column, or its event type binding), which has no
dependencies route of its own.

- `certainty` is `direct` for a stored reference (an id, or a column name read
  in the entity's own scope) and `possible` for a match by name without a
  stored id: an SQL identifier or JSON-key literal in a `sql` metric's query,
  filter SQL or a fact table's SQL, a fact-table or `fact` metric column, a
  property binding by column name, a column on a scan with no event type. Treat
  `possible` as "check it", never as proof.
- `url_hint` is the entity's path in the app, without `?branch=`. It is filled
  for every kind except a field whose event type cannot be found, where it is
  `null`.
- `depth` is `1` for a neighbour of the asked entity and `2` for a neighbour of
  a neighbour (only with `depth=2`).
- `counts_by_kind` counts the direct, depth-1 downstream edges;
  `possible_counts_by_kind` counts the rest of `downstream` (possible matches
  and depth-2 edges).

See [Dependencies & impact](../use/dependencies-and-impact.md#what-counts-as-a-dependency)
for every edge, including *superseded by* links between events, detection
overrides, properties used in field and meta values, and scan drift, platform
and app-version columns.

Ask about a set of planned changes at once:

```http
POST /api/v1/projects/{slug}/impact
```

```json
{
  "changes": [
    { "kind": "event", "id": "5a1f…", "change": "delete" },
    { "kind": "variable", "id": "77aa…", "change": "rename" }
  ]
}
```

`changes` holds 1 to 200 items; more is a `422`. A caller sends `change` as
`delete`, `deprecate` or `rename`. Every change is resolved at depth 1. The
response has one item per change:

```json
{
  "items": [
    {
      "change": { "kind": "event", "id": "5a1f…", "change": "delete" },
      "entity": { "kind": "event", "id": "5a1f…", "name": "checkout:completed",
                  "exists": true },
      "name": "checkout:completed",
      "affected": [
        { "kind": "metric", "id": "c3d2…", "name": "Checkout conversion",
          "relation": "metric uses event in its composition",
          "certainty": "direct",
          "url_hint": "/p/shop/monitoring/metric/c3d2…", "depth": 1 }
      ],
      "summary": "1 metric"
    }
  ]
}
```

`name` repeats `entity.name` for list rows. It writes nothing. Use `summary`
when you report to a person (possible matches are counted apart, as in
*1 metric, plus 1 fact table that may use it*), and `affected` when you decide
what else to update first.

Get the same answer for everything a plan branch changed:

```http
GET /api/v1/projects/{slug}/branches/{branch_id}/impact
```

The response has the same `items` shape as `POST /impact`, with the change set
taken from the branch's diff: deleted, renamed, deprecated or archived, and
otherwise edited events, event types, fields and properties. Here `change` can
also be `change`, a response-only value for an entity edited in place (a
field's type, an event's breakdown columns) without being renamed, deprecated or
archived. A rename appears once, paired the way the diff's `renames` list pairs
it; additions are left out. This is what the branch's **Impact** panel shows;
an agent reviewing a branch can read it before approving.

## Duplicates and naming {#duplicates-and-naming}

Before an agent creates events, it can ask whether they already exist under a
near name and whether the names follow the project's convention. No language
model is involved. In short:

- A **lexical score** in 0..1 over normalised tokens: the higher of token
  Jaccard and character-trigram Dice, raised to 0.95 when one name only adds
  filler words (`screen`, `page`, `button`, ...), capped at 0.75 when the same
  words appear in another order (unless only an action verb moved), and capped
  at 0.5 when each name has a number the other lacks.
- Under an event type's naming rule, two names of that type are scored **slot
  by slot** and take the weakest slot's score. Every literal between two
  placeholders of the rule is a separator.
- When the deployment has `SEARCH_EMBEDDINGS_ENABLED` and vectors exist, the
  combined score is `max(lexical, 0.5 × lexical + 0.5 × cosine)`. A pair needs
  a lexical score of at least 0.76 before an embedding is considered, and a
  slot-by-slot pair must reach the threshold lexically. Otherwise the score is
  the lexical score.
- A pair is a likely duplicate at **0.88** or above.

See [Duplicates & naming](../use/duplicates-and-naming.md) for the full rules,
convention inference and the lint codes.

All three routes follow the project's membership, and plan reads resolve on the
branch named by the usual `branch` query parameter, or on main without one.

**Check candidates** (any member, viewers and `read` API keys included):

```http
POST /api/v1/projects/{slug}/events/duplicate-check
```

```json
{
  "candidates": [
    {
      "name": "paywall_screen_view",
      "event_type_id": "91c0…",
      "description": "Paywall shown",
      "field_values": [
        { "field_definition_id": "c4e8…", "value": "onboarding" }
      ]
    }
  ]
}
```

It is a `POST` only because the list can be long (1 to **500** candidates); it
writes nothing, and it is allowlisted as a read in the role checks and the
mutation audit. `event_type_id` is required. `description` and `field_values`
are optional. Under a naming rule, `name` may be empty and the name is rendered
from `field_values`. Send `event_id` when checking an event that already exists,
so it is not reported as its own duplicate.

The answer has one item per candidate, in the order sent:

```json
{
  "items": [
    {
      "name": "paywall_screen_view",
      "duplicates": [
        {
          "event_id": "5a1f…",
          "name": "paywall_view",
          "event_type_id": "91c0…",
          "status": "live",
          "score": 0.95,
          "reasons": ["similar name", "same event type", "1 shared field value"]
        }
      ],
      "lint": [],
      "suggestion": null,
      "lint_applicable": true,
      "convention": {
        "case": "snake",
        "space_style": null,
        "separator": null,
        "verb_position": "last",
        "prefix": null,
        "confidence": 0.97,
        "sample_size": 184
      }
    }
  ],
  "threshold": 0.88,
  "semantic_used": false
}
```

- `name` is the name that was checked: the candidate's own, or the one its
  naming rule rendered.
- `duplicates` holds at most three matches at or above `threshold`. Neighbours
  are the branch's non-archived events of every type; matches of the
  candidate's own event type come first, then the others, each by score.
  `reasons` uses these strings: `same name`, `similar name` (the lexical score
  alone reaches the threshold), `semantic match` (cosine 0.85 or more),
  `same event type` or `different event type`, and `N shared field value` /
  `N shared field values`.
- `lint` is one entry per departure from the inferred convention, each
  `{ "code", "message", "suggestion" }`; `code` is `case`, `separator`,
  `verb_order` or `prefix`.
- `suggestion` is one name that fixes every lint entry at once, or `null`.
- `lint_applicable` is `false` for an event type with a naming rule: the rule
  decides the spelling, so `lint` is empty and `convention` is `null`.
  `convention` is the inferred convention (with the type's prefix) otherwise.
- `semantic_used` is `true` when embedding cosines took part in the scores.
  Candidates are embedded only for requests of up to 50 candidates.

Treat the answer as advice. The create routes do not consult it, and a warned
candidate still creates; the existing `409` for a taken scan identity is
unchanged.

**List duplicate clusters** (any member):

```http
GET /api/v1/projects/{slug}/duplicates?cursor=<cursor>
```

Groups the branch's `live`, `implemented` and `ready_for_dev` events into
clusters. Only events of the same event type are compared; pairs at or above
the threshold are joined transitively, and dismissed pairs never link. The
clusters come 25 per page, strongest first; pass `next_cursor` back as `cursor`
for the next page.

```json
{
  "items": [
    {
      "events": [
        { "id": "5a1f…", "name": "paywall_view", "status": "live",
          "event_type_id": "91c0…", "volume_7d": 18230 },
        { "id": "77b2…", "name": "paywall_screen_view", "status": "live",
          "event_type_id": "91c0…", "volume_7d": 412 }
      ],
      "score": 0.95
    }
  ],
  "next_cursor": "25",
  "total": 31,
  "threshold": 0.88,
  "truncated": false
}
```

A cluster's `score` is its strongest pair, and at most 20 of its events are
listed. `total` counts the clusters on all pages. `truncated` is `true` when the
answer is partial: the branch has more than 5,000 such events, or scoring
stopped at 200,000 pairs. A malformed cursor answers `422`.

**Dismiss a pair** (editor):

```http
POST /api/v1/projects/{slug}/duplicates/dismiss
```

```json
{ "event_a_id": "5a1f…", "event_b_id": "77b2…" }
```

```json
{ "event_a_id": "5a1f…", "event_b_id": "77b2…", "created": true }
```

The pair is stored once per project, against the main events that branch
copies came from, so it holds on every branch. Either order of the two ids
dismisses the same pair, and the response gives the stored pair in its fixed
order. Sending it again answers `created: false`. The two ids must differ (`422`)
and both must be events of the project (`404`). The dismissal is recorded in
the audit log as `event.duplicate_dismiss`.

**Merging is not a route.** To merge a duplicate into the event you keep, patch
the one you retire through the existing event update:

```http
PATCH /api/v1/projects/{slug}/events/{event_id}?branch=<branch_id>
```

```json
{ "status": "deprecated", "superseded_by_event_id": "5a1f…" }
```

It is an ordinary plan edit: branch rules apply, nothing is re-pointed and no
volume moves. Check [dependencies](#dependencies-and-impact) first, and see
[Event lifecycle](#event-lifecycle) for how the retirement is then watched.

## Event lifecycle {#event-lifecycle}

Some lifecycle facts come from the data rather than from an edit. They are
written on `main` only and never through a branch — see
[Going live on its own](../use/feature-reference.md#going-live) and
[Sunset watch](../use/feature-reference.md#sunset-watch).

**Going live.** The first scan that sees an event with volume sets its
`first_seen_at` (once; `null` until then) and, for a `ready_for_dev` or
`implemented` event whose required fields all have a non-empty value, moves
`status` to `live`. The move is not a plan write: it bypasses branch rules, and
the event's `/history` records it as a `status` row whose `author_label` is
`tripl (scan)` and whose `user_id` / `user_email` are `null`. The label comes
from the change's recorded source (a scan), not from the missing user, so read
`author_label` rather than inferring a scan from `user_id` being `null`; a
person's edit carries `author_label: null` and names the person in
`user_email`. An event with a required field left blank is not promoted, however
much traffic arrives — this is a change from earlier releases, which promoted on
volume alone. When the event has an implementation
ticket, the ticket gets a comment saying when the event was seen. Do not try to
reproduce this by `PATCH`ing `status` to `live` yourself — an agent's edit is a
plan change and goes through the branch like any other.

**Lifecycle findings.** A daily check records open problems with retirements:

```http
GET /api/v1/projects/{slug}/lifecycle-findings
```

The response is `{"items": [...], "total": N}`, open findings only;
`?include_resolved=true` adds the closed ones. Each item:

| Field | Meaning |
|-------|---------|
| `id` | The finding's id. |
| `event_id` / `event_name` | The **deprecated** event the finding hangs on — for both kinds, including `successor_silent`. |
| `related_event_id` / `related_event_name` | For `successor_silent`: the successor that has gone quiet. `null` for `sunset_overdue`. |
| `kind` | `sunset_overdue` — a deprecated event past `sunset_at` that still had volume in the last 24 hours; `successor_silent` — the successor of a deprecated event (the event its `superseded_by_event_id` names) had no volume in the last 7 days. |
| `first_seen_at` / `last_seen_at` | The first and the latest daily check that found the condition. |
| `resolved_at` | When the condition cleared; `null` while the finding is open. |
| `volume_24h` | For `sunset_overdue`: the old event's count over the last 24 hours. |
| `successor_volume_7d` | For `successor_silent`: the successor's count over the last 7 days. |

The same findings appear on the deprecated event itself as `lifecycle_findings`
in `GET /projects/{slug}/events/{event_id}`, and each events-list item carries a
boolean `lifecycle_warning` that is true while the event has an open finding —
enough to flag it in a listing without a second call. Because every finding
hangs on the deprecated event, the flag is on the deprecated event for both
kinds; a silent successor is not itself flagged. A finding is updated in
place by each check and resolved when its condition clears, so a list of open
findings is the current state, not a log. Rules alert on open findings when
`include_lifecycle` is true — see
[Lifecycle alerts](../use/alerting.md#lifecycle).

**Successor adoption.** For a deprecated event that names a successor:

```http
GET /api/v1/projects/{slug}/events/{event_id}/migration
```

```json
{
  "old": { "event_id": "…", "name": "checkout_v1", "daily_avg_7d": 1240 },
  "new": { "event_id": "…", "name": "checkout_completed", "daily_avg_7d": 3800 },
  "ratio": 3.06
}
```

`daily_avg_7d` is each event's average daily volume over the last 7 days; a
collected bucket that straddles either edge of the window counts in proportion
to the part of it inside the window. `ratio` is new over old — how many times
the old event's volume the successor now receives (here 3,800 / 1,240 ≈ 3.06) —
and is `null` when the old event's average is `0`, where no ratio exists. Use it to judge whether a retired event can be
archived: a successor well ahead of the old event and an old event near zero is
a migration that has landed. The route answers only for a deprecated event with
a successor.

**Implementation tracker.** `GET`/`PATCH /api/v1/projects/{slug}/tracker-config`
(owner-only for writes) takes `tracker_type` `jira` or `linear`. Linear needs a
`team_id` and the API key; as with Jira's token the key is write-only, encrypted
at rest, and reported back only as whether one is set. Switching
`tracker_type` between `jira` and `linear` clears the stored credential (and the
Jira project key), since a Jira token must never be sent to Linear or the other
way round: send the new tracker's token or key in the same `PATCH`, or the
tracker is left without one. Tickets are read the same
way for both trackers, from the branch and event `implementation-tickets` routes.

## Health score {#health-score}

Every event of the **main** plan whose status is not `archived` has a health
score from 0 to 100. How it is built (fixed weights, which components are
excluded and when, the worked example) is in
[Health score](../use/health-score.md). All routes below are reads: any project
member, viewers included, may call them. They take no branch parameter and
always answer about the main plan.

**One event:**

```http
GET /api/v1/projects/{slug}/events/{event_id}/health
```

```json
{
  "event_id": "…",
  "event_type_id": "…",
  "name": "checkout_completed",
  "score": 60,
  "grade": "warning",
  "renormalized": true,
  "excluded": ["signals"],
  "top_issue": "Last seen 10d ago",
  "components": [
    {
      "key": "implemented_seen",
      "label": "Implemented & seen",
      "weight": 25,
      "applies": true,
      "excluded_reason": null,
      "value": 0.5,
      "effective_weight": 29.4,
      "points": 14.7,
      "detail": "Last seen 10d ago",
      "counts": {"days_since_seen": 10}
    },
    {
      "key": "signals",
      "label": "Signals",
      "weight": 15,
      "applies": false,
      "excluded_reason": "Anomaly detection is off for its scans",
      "value": null,
      "effective_weight": null,
      "points": null,
      "detail": "Anomaly detection is off for its scans",
      "counts": {}
    }
  ]
}
```

`components` always has six entries, in this order: `implemented_seen`,
`contract`, `drifts`, `signals`, `freshness`, `documentation` (the example
shows two). `weight` is the fixed weight; `effective_weight` is the component's
share in percent after renormalization and `points` is `effective_weight ×
value`, both `null` when the component is excluded. `grade` is `healthy` (80
and above), `warning` (50 to 79) or `unhealthy` (below 50). `renormalized` is
true when at least one component is excluded, and `excluded` lists them.
`top_issue` is the `detail` of the component that loses the most points, `null`
when nothing loses any. `counts` carries the numbers behind `detail`, per
component:

| Component | `counts` keys |
|-----------|---------------|
| `implemented_seen` | `days_since_seen`, or `sunset_overdue` / `successor_silent` for a deprecated event with that open finding |
| `contract` | `violated`, `total` |
| `drifts` | `schema`, `value`, `distribution` |
| `signals` | `open` |
| `freshness` | `sources`, `late`, `overdue` |
| `documentation` | `has_description`, `has_owner` (0 or 1) |

The route answers `404` for an event on a branch, an `archived` event, or an id
that is not in the project.

**Many events:**

```http
GET /api/v1/projects/{slug}/health/events?ids={id1}&ids={id2}
```

Repeat `ids` for each event, 1 to 150 per call (more, or none, is `422`).
The cap keeps the URL near 6 KB, under the 8 KB request line a proxy with
nginx's default buffers accepts; for more events, split them over several calls.
The response is `{"items": [...], "computed_at": "…"}`: one item per scored
event in the order you asked, with the shape above. Duplicate ids are answered
once, and ids outside the scored population (a branch copy, an archived event,
another project's event) are left out rather than failing the call.

**Event types:**

```http
GET /api/v1/projects/{slug}/health/event-types
```

The response is `{"items": [...]}`, one item per event type of the main plan,
including types with no scored events:

| Field | Meaning |
|-------|---------|
| `event_type_id` | The event type. |
| `score` / `grade` | Mean of its events' scores, rounded half up; `null` when it has no scored events. |
| `scored_events` | How many of its events are scored. |
| `healthy_count` / `warning_count` / `unhealthy_count` | Its events per grade. |
| `component_averages` | Six `{key, value, applies_count}` entries: the mean component value over the events it applies to (`null` when it applies to none) and how many that is. |
| `worst` | Up to five `{event_id, name, score, grade, top_issue}`, lowest score first, then by name. |

**The plan:**

```http
GET /api/v1/projects/{slug}/health?trend_days=30
```

The same fields as an event-type item (without `event_type_id`), over every
scored event of the project, plus:

| Field | Meaning |
|-------|---------|
| `trend` | `{day, score, scored_events}` from the daily snapshots of the last `trend_days` days (1 to 365, default 30), oldest first. A day with no snapshot has no point. |
| `previous_score` | The snapshot score from 7 days before today, or `null` when there is none. |
| `computed_at` | When the score was computed. |

The score part is live, but the response is cached for up to two minutes; the
snapshots behind `trend` are written once a day at 05:55 UTC.

**Catalog order.** `GET /api/v1/projects/{slug}/events?order_by=health` lists
the filtered events least healthy first (score ascending, then name, then id);
events with no score sort after all scored ones. `total` and the filters work
as for any other order. The request with `offset=0` scores the filtered set
and fixes its order for 60 seconds; later pages with the same filters slice
that order, so paging neither repeats nor skips an event while scores change.
Ask for `offset=0` again to re-sort. On a branch the call answers `400` with
`Health sort is available on the main plan`. The other values of `order_by`
are `catalog` (the default) and `volume`.

## Notifications and subscriptions {#notifications}

Every user has an in-app notification list, email preferences, and a set of
subscriptions (watches) on events, event types, metrics and branches. A key
acts as the user who created it, so these routes read and change **that
user's** notifications and subscriptions. The product behaviour is described
in [Notifications & watching](../use/notifications.md).

### Reading notifications

```http
GET /api/v1/me/notifications?unread=true&limit=30&cursor=…
GET /api/v1/me/notifications/unread-count
```

The list covers every project the user is currently a member of, newest
first. `unread=true` returns only unread notifications; `limit` is the page
size (default 30, at most 100). The list comes back as a page:

```json
{"items": [ … ], "next_cursor": "…"}
```

Pass `next_cursor` back as `cursor` to get the next (older) page; it is
`null` on the last page. Treat the cursor as opaque. Each item carries:

| Field | Meaning |
|-------|---------|
| `id` | notification id |
| `project_id`, `project_slug`, `project_name` | the project it belongs to |
| `kind` | `comment`, `reply`, `mention`, `open_question`, `signal`, `branch_review_requested`, `branch_approved`, `branch_merged` or `lifecycle` |
| `entity_type`, `entity_id` | what it is about: `event`, `event_type`, `metric` or `branch`, and its id |
| `title`, `body` | a short title and text |
| `url` | the in-app path to open |
| `actor` | who caused it, as `{"id", "name", "email"}`, or `null` (for example a signal, or a deleted user) |
| `read_at` | when it was marked read, or `null` |
| `created_at` | when it was created |

`unread-count` returns `{"unread": 3}`.

Mark notifications read, either by id (up to 500 per call) or all at once;
the body must name one or the other:

```http
POST /api/v1/me/notifications/read
```

```json
{"ids": ["3f2a…", "9b41…"]}
```

```json
{"all": true}
```

The response reports how many notifications changed and the unread count
left: `{"updated": 2, "unread": 1}`.

### Email preferences

```http
GET   /api/v1/me/notification-prefs
PATCH /api/v1/me/notification-prefs
```

```json
{"email_mode": "weekly", "mentions_email": true}
```

`email_mode` is `off`, `instant`, `daily` (the default) or `weekly`;
`mentions_email` (default `true`) controls email for mentions separately from
`email_mode`. Both fields are optional in a `PATCH`. The response also carries
`email_available`, which is `false` when the instance has no SMTP configured:
the preferences are kept, but nothing is emailed until SMTP is set up, and the
in-app list is unaffected. Instant emails go out within about a minute. Daily
and weekly digests group the notifications that are unread and not yet
emailed, and each notification is emailed at most once.

### Subscriptions

A subscription is per user and entity, inside a project:

```http
GET    /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
PUT    /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
DELETE /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
PATCH  /api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}
```

`entity_type` is `event`, `event_type`, `metric` or `branch`. `GET` returns the
caller's own state for that entity; `PUT` watches it (adds the `manual`
reason); `DELETE` unwatches it; `PATCH` sets `muted`:

```json
{"muted": true}
```

All four return the same body, the caller's subscription state:

```json
{
  "entity_type": "event",
  "entity_id": "7c1e…",
  "watching": true,
  "muted": true,
  "reasons": ["author", "commenter"]
}
```

`entity_id` is the id the subscription is kept under. For an event that is the
event's discussion home, so watching an event's copy on a plan branch returns
the id of its main-plan twin. `reasons` lists why the subscription exists:
`author`, `owner`, `commenter`, `reviewer` and/or `manual`. tripl adds the
automatic reasons itself (event author on create, event type owners through the
`event_type` subscription, the first comment on an event, branch author and
reviewers).

A muted subscription stays in place: it still reads `watching: true` with
`muted: true` and keeps its reasons, but produces no notifications except
mentions, which always get through. `DELETE` removes the subscription
entirely, muted or not.

Watching an event type brings the signals, lifecycle findings and open
questions on its events, not their ordinary comments and replies. To follow an
event's discussion, watch the event itself.

### Mentions

A comment body mentions a member with `@[Name](user_id)`. Only that form
notifies; plain `@name` text does not. Take the ids from
[`GET /api/v1/projects/{slug}/members`](#project-members), or, for members who
hold the organization's default access without a row, from the organization's
member list. A mentioned user who cannot see the project is skipped. A mention notifies even when the
mentioned user has muted the thread.

### Who is notified

| `kind` | Recipients |
|--------|------------|
| `comment`, `reply` on an event | the event's watchers |
| `comment`, `reply` on a branch | the branch's watchers |
| `mention` | the mentioned members |
| `open_question` | the event's author and its event type's owners |
| `signal` | watchers of the event, event type or metric; a signal on an event also reaches its event type's watchers |
| `lifecycle` | watchers of the event and of its event type |
| `branch_review_requested` | the requested reviewers |
| `branch_approved`, `branch_merged` | the branch's author, its reviewers and its watchers |

A user who already got a `mention` for a comment does not also get the
`comment` or `open_question` for it. Recipients are checked against the
project's current members when a notification is created and again before it
is emailed, and the user whose action caused it is never notified. Signal
notifications are limited to one per new signal, at most one per entity per
subscriber every 6 hours, and are never created for hidden or verdicted
signals.

## Plan validation {#plan-validation}

Check tracking calls or captured events against the plan in one batch. This is
the validator behind [`tripl check`](./tripl-check.md). Use it directly when
your own tool already knows what an event looks like, for example a test
harness that records what the app sent.

```http
POST /api/v1/projects/{slug}/plan/validate?branch=<branch_id>
```

The route is read-like. It writes nothing, and it is a `POST` only to carry the
batch. Any project member can call it, viewers included, and a `read` key is
enough. Without `branch` it validates against main. With `branch`, it validates
against that branch's plan, with the usual `400` / `404` for a malformed or
foreign id.

```json
{
  "items": [
    {
      "ref": "App/Checkout/PayButton.swift:42:9",
      "event_type": "se",
      "name": null,
      "fields": { "category": "checkout", "action": "tap", "label": "pay_button", "plan": null },
      "properties": { "source": "cart" }
    },
    {
      "ref": "payload:7",
      "event_type": null,
      "name": "promo_banner_shown",
      "fields": {},
      "properties": { "slot": "homepage" },
      "complete": true
    }
  ],
  "strict": false
}
```

The body has two top-level keys:

| Key | Meaning |
|-----|---------|
| `items` | 1 to 5,000 items. More is a `422`, so split a larger batch. |
| `strict` | Optional, default `false`. `true` adds a `dynamic_value` `info` finding for every value that is only known at runtime. `tripl check --strict` sets it. |

Each item has these keys:

| Key | Meaning |
|-----|---------|
| `ref` | Optional. Any string. It is echoed back so you can match verdicts to your inputs, for example `file:line` or a payload line number. |
| `event_type` | The plan event type **name**, or `null`. |
| `name` | The event name or identity, or `null`. |
| `fields` | Plan field name to value: a string, number, boolean or `null`. In a call-site item (`complete` false) a `null` value means *dynamic*: the value is unknown at scan time, and a dynamic value is never an error. |
| `properties` | Optional. Other keys the event carries. Each key is checked as a field name of the type, and each literal value like a field value. |
| `complete` | Optional, default `false`. `true` says this is a whole event, as sent, rather than one call site. Only then is a required field that is absent reported, and the item's values are taken as sent: they are never holes. |

How an item is resolved:

- **With `event_type` and `fields`**: the server builds the identity with the
  type's name rule (its resolved `event_name_format`, for example
  `{category}:{action}:{label}` gives `checkout:tap:pay_button`) and looks up
  the event with that identity within the type. A `null` field the rule needs
  becomes a `${key}` **hole**: `checkout:${action}:pay_button`. A type with no
  name rule uses `name` as the identity.
- **With only `name`**: it is matched against event identities and names across
  every type. `${…}` tokens in the name (from interpolation in code) are holes
  too.

A hole matches whatever the plan has in that place, and the plan's own
`${variable}` placeholders match the item's literal text (which is then checked
against the property's documented values). The fields an item carries are
checked against the type whether or not the identity matched. When the identity
matches no planned event:

- **Fully literal**: `unknown_event`, `error`.
- **With holes and some literal text**: `unknown_event`, `warning`. Nothing
  readable matches, but the runtime value might.
- **Only holes and separators** (`${category}:${action}:${label}`): nothing is
  matched and nothing is reported. `event_id` is `null`.
- **More than 10 holes**: `too_dynamic`, `info`. The identity is not matched.

A `complete: true` item never has holes, so an unmatched identity there is
always an error.

The response has one verdict per item, in the order of the request:

```json
{
  "items": [
    {
      "ref": "App/Checkout/PayButton.swift:42:9",
      "status": "warning",
      "event_id": "5a1f…",
      "identity": "checkout:tap:pay_button",
      "findings": [
        { "code": "unknown_field", "severity": "warning", "field": "source",
          "message": "'source' is not a field of event type 'se'" }
      ]
    },
    {
      "ref": "payload:7",
      "status": "error",
      "event_id": "9c02…",
      "identity": "promo_banner_shown",
      "findings": [
        { "code": "value_not_allowed", "severity": "error", "field": "slot",
          "message": "'homepage' is not an allowed value of slot (home, cart)." },
        { "code": "missing_required_field", "severity": "error", "field": "variant",
          "message": "Required field 'variant' is missing" }
      ]
    }
  ],
  "summary": { "ok": 0, "warnings": 1, "errors": 1 }
}
```

An item's `status` is `ok`, `warning` or `error`, the worst severity among its
findings. `summary` counts items by status. The finding codes are:

| Code | Severity | When |
|------|----------|------|
| `unknown_event_type` | error | `event_type` names no event type in the plan. |
| `unknown_event` | error, or warning | No event has the built identity, or the given name. An error when the identity is fully literal, a warning when it has holes. An identity of holes alone gets no finding. |
| `deprecated_event` | warning, or error | The matched event is `deprecated` (warning) or `archived` (error). |
| `unknown_field` | warning | A `fields` or `properties` key that the event type does not define. |
| `missing_required_field` | error | Only for `complete: true`: a required field of the type is absent. |
| `value_not_allowed` | error | A literal value is outside the field's enum options, outside the documented `allowed_values` of the property the field refers to, or fails the field's contract regex or min/max. |
| `dynamic_value` | info | Only with `"strict": true`: a field was sent as `null` or with a hole, or the identity has holes. |
| `too_dynamic` | info | The identity has more than 10 holes, too many to match. |

An `info` finding never changes an item's `status`.

`field` names the plan field a finding is about, or is `null` for findings about
the whole item. `message` is prose and may change. Select on `code`.

## Plan export {#plan-export}

Read the whole plan in a form built for generating or validating code. This is
the endpoint behind [`tripl codegen`](./codegen.md) and
[`tripl export`](../run/cli.md#tripl-export), and behind the **Export JSON
Schema** button on the Plan history page.

```http
GET /api/v1/projects/{slug}/plan/export?format=jsonschema&branch=<branch_id>
GET /api/v1/projects/{slug}/plan/export?format=codegen_model&branch=<branch_id>
```

It is a read and changes nothing. Any project member can call it, viewers
included, and a `read` key is enough. Without `branch` it exports main. With
`branch`, it exports that branch's plan, with the usual `400` / `404` for a
malformed or foreign id. `format` defaults to `jsonschema`; an unknown format
is a `422`.

Both formats leave out **archived** events and include **deprecated** ones,
flagged. Both carry the same header keys, so a generated file can say what it
was generated from:

| Key | Meaning |
|-----|---------|
| `format` | `jsonschema` or `codegen_model`, as requested. |
| `revision` | The plan revision the export reflects: the latest revision for main, the base revision for a branch. `null` when no revision was ever taken. |
| `branch` / `branch_id` | The branch the plan was read from, by name (`main` for main) and id. |
| `plan_hash` | `sha256:…` over the exported content. It changes exactly when the export would, so a CI job can compare it instead of the whole body. |

There is no timestamp: the same plan exports byte-identical.

### `format=jsonschema` {#plan-export-jsonschema}

One [JSON Schema](https://json-schema.org/draft/2020-12) document per event,
keyed `<event_type>/<identity>`:

```json
{
  "format": "jsonschema",
  "revision": "3f9c2a1e-…",
  "branch": "main",
  "branch_id": "0b7d…",
  "plan_hash": "sha256:9e41…",
  "schemas": {
    "se/checkout:tap:pay_button": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "checkout:tap:pay_button",
      "type": "object",
      "properties": {
        "category": { "type": "string", "const": "checkout" },
        "action": { "type": "string", "const": "tap" },
        "label": { "type": "string", "const": "pay_button" },
        "plan": { "type": "string", "enum": ["annual", "monthly"] },
        "coupon": { "type": "string", "pattern": "^[A-Z0-9]{6,12}$" },
        "cart_value": { "type": "number", "minimum": 0, "maximum": 100000 }
      },
      "required": ["category", "action", "label"],
      "x-tripl": {
        "event_type": "se",
        "identity": "checkout:tap:pay_button",
        "name": "checkout:tap:pay_button",
        "status": "live"
      }
    },
    "track/Home Screen View": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "Home Screen View",
      "type": "object",
      "properties": {
        "platform": { "type": "string", "enum": ["android", "ios"] },
        "source": { "type": "string" }
      },
      "required": ["platform"],
      "x-tripl": { "event_type": "track", "identity": "Home Screen View", "name": "Home Screen View", "status": "live" }
    }
  }
}
```

Two events of one type with the same identity keep both schemas; the second is
keyed `…#2`.

How a field becomes a property:

| Plan | Schema |
|------|--------|
| Field type | `string`, `enum` → `type: string`; `url` → `type: string, format: uri`; `number` → `type: number`; `boolean` → `type: boolean`; `json` → no `type` (anything). |
| Required field | Listed in `required`. |
| The event's value is a literal (`checkout`, `9.99`) | `const`, typed by the field type: a number field's `"9.99"` is the number `9.99`. |
| The event's value is a whole `${variable}` | `enum` of the property's allowed values (the event's own override list when it has one). No allowed values: no constraint. |
| The event's value is a template (`item_${kind}`) on a string field | An anchored `pattern`, each hole an alternation of the allowed values, or `.*`. |
| Enum field | `enum` of its options. |
| Contract regex | `pattern`. Unanchored: the same partial match `tripl check` and the drift job apply. |
| Contract min, max | `minimum`, `maximum`, on number fields only. |

When the event's value and a contract both set the same keyword (for example a
`${variable}` `enum` on an enum field), the contract's copy goes into `allOf`,
so both hold. A field's
display name and description become its `title` and `description`, an event's
description becomes the schema's `description`, and a deprecated event has
`"deprecated": true`. The `x-tripl` object is an annotation (event type,
identity, name, status); a 2020-12 validator ignores it.

### `format=codegen_model` {#plan-export-codegen-model}

The plan as the code generator needs it: every event type with its name rule,
fields and events, and the documented properties.

```json
{
  "format": "codegen_model",
  "revision": "3f9c2a1e-…",
  "branch": "main",
  "branch_id": "0b7d…",
  "plan_hash": "sha256:51c0…",
  "event_types": [
    {
      "name": "se",
      "display_name": "Structured events",
      "name_rule": "{category}:{action}:{label}",
      "fields": [
        { "name": "category", "required": true, "type": "string", "values": null, "variable": null },
        { "name": "plan", "required": false, "type": "enum", "values": ["annual", "monthly"], "variable": null }
      ],
      "events": [
        {
          "identity": "promo_sheet:tap:${promo_slot}",
          "name": "promo_sheet:tap:${promo_slot}",
          "status": "live",
          "field_values": { "category": "promo_sheet", "action": "tap", "label": "${promo_slot}" },
          "deprecated": false,
          "overrides": {}
        }
      ]
    }
  ],
  "variables": [
    { "name": "promo_slot", "allowed_values": ["cart", "home"], "tokens": ["promo_slot"] }
  ]
}
```

| Key | Meaning |
|-----|---------|
| `event_types[].name_rule` | The type's resolved event name format, or `null` for a type identified by a flat name. |
| `fields[].type` | The plan field type: `string`, `number`, `boolean`, `json`, `enum` or `url`. |
| `fields[].values` | The closed set of values the plan allows for the field across the type's events, or `null` when it is free. It is closed only for a string-like field that **every** exported event fills with a literal, a property with allowed values, or a template whose holes all have them; an event that leaves the field unset makes it free. A free enum field falls back to its options. |
| `fields[].variable` | The property the field is bound to, or `null`. |
| `events[].field_values` | Plan field to value. A `${token}` value is a property placeholder; its values are in `variables`. |
| `events[].deprecated` | `true` for a deprecated event. Archived events are not listed. |
| `events[].overrides` | The event's own allowed values for a property, keyed by every `${token}` spelling of it, values in plan order. For that event only they replace the property's `allowed_values`; an empty list means the event accepts any value. `{}` when the event overrides nothing. |
| `variables[].allowed_values` | The property's documented values. |
| `variables[].tokens` | Every `${token}` spelling that names the property, so a stored `field_values` template can be mapped back to it. |

## Docs catalog {#docs-catalog}

The docs catalog holds Markdown notes for people and agents: warehouse
gotchas, event query recipes, agent skills. See
[Docs catalog](../use/docs-catalog.md) for the rules on paths, frontmatter,
links and limits. Every route is under the project, and `scope` picks the
project's own notes (`project`) or its organization's (`organization`):

```http
GET    /api/v1/projects/{slug}/docs
GET    /api/v1/projects/{slug}/docs/file?scope=project&path=guides/warehouse.md
GET    /api/v1/projects/{slug}/docs/search?q=double%20count&scope=project&limit=20
GET    /api/v1/projects/{slug}/docs/revisions?scope=project&path=guides/warehouse.md
GET    /api/v1/projects/{slug}/docs/revisions/{revision_id}
GET    /api/v1/projects/{slug}/docs/backlinks?kind=field&name=amount&qualifier=checkout
GET    /api/v1/projects/{slug}/docs/links?ref=event:purchase&ref=field:checkout/amount
GET    /api/v1/projects/{slug}/docs/link-suggestions?q=signup&kind=metric&limit=8
GET    /api/v1/projects/{slug}/docs/export?scope=project&format=json
PUT    /api/v1/projects/{slug}/docs/file?scope=project&path=guides/warehouse.md
DELETE /api/v1/projects/{slug}/docs/file?scope=project&path=guides/warehouse.md
DELETE /api/v1/projects/{slug}/docs/folder?scope=project&path=guides
POST   /api/v1/projects/{slug}/docs/move
POST   /api/v1/projects/{slug}/docs/revisions/{revision_id}/restore
POST   /api/v1/projects/{slug}/docs/import?scope=project&mode=merge&dry_run=true
POST   /api/v1/projects/{slug}/docs/import/zip?scope=project&mode=merge&dry_run=true&keep_root=false
GET    /api/v1/projects/{slug}/docs/file/sharing?scope=project&path=guides/warehouse.md
PUT    /api/v1/projects/{slug}/docs/file/sharing?scope=project&path=guides/warehouse.md
GET    /api/v1/projects/{slug}/docs/folder/sharing?scope=project&path=guides/
PUT    /api/v1/projects/{slug}/docs/folder/sharing?scope=project&path=guides/
```

Reads (every `GET`) are open to any project member, including viewers and
`read`-scope keys. A non-member gets `404`. Writes need an editor on the
project and a `write`-scope key. Organization notes are readable from every
project of the organization, so only an owner or admin of the organization may
change them (`403` for anyone else), and a key bound to one project cannot
change them (`403`). Deleting organization notes in bulk, with
`DELETE /docs/folder` or an import in `mirror` mode, needs an organization
owner or admin in a browser session: every API key gets `403`. Two writers racing on the same note get `409`, as a stale
`base_revision` does.

`GET /docs` returns the tree: `project_docs` and `organization_docs` (each
note's `scope`, `path`, `title`, `description`, `tags`, `audience`,
`revision`, `size_bytes`, `updated_at`, `updated_by_name`, `visibility`,
`my_permission` and `shared`), the project and
organization, and the `limits`. `GET /docs/file` adds `id`, the raw `content`
(frontmatter included), the `body` without frontmatter, `extra_frontmatter`
(the keys tripl does not interpret), `links` and `linked_from`. Each link has
a `status` of `resolved`, `ambiguous`, `broken` or `unavailable`, plus the
in-app `route_path` of its target on the main plan. A link that does not
resolve may have a `reason`: `not_found`, `invalid_id`, `path_form` (a
`[[doc:path]]` that was not saved as an id) or `not_a_member` (a mention of
someone outside the organization). A broken link by name also has
`suggestions`, up to three current names close to the one written. Only the
first 20 broken links of one response get suggestions; the others have an
empty list.
`linked_from` lists the notes that link to this one (`scope`, `path`,
`title`), limited to notes the caller can read. A missing note is `404` with
`"Doc not found"`.

### Link syntax {#docs-link-syntax}

| Kind | Syntax | Resolved by |
| --- | --- | --- |
| `doc` | `[[doc:<id>]]`, `[[doc:<id>#heading-slug\|label]]` | note id; shown with the note's current title |
| `event` | `[[event:NAME]]` | name, on the main plan |
| `event_type` | `[[event-type:NAME]]` | name, on the main plan |
| `field` | `[[field:NAME]]`, `[[field:EVENT_TYPE/NAME]]` | name, on the main plan |
| `variable` | `[[variable:NAME]]` | name, on the main plan |
| `metric` | `[[metric:NAME]]` | catalog metric name |
| `alert_rule` | `[[alert-rule:<id>]]` | rule id; shown with the rule's current name |
| `branch` | `[[branch:NAME]]` | plan branch name |
| `scan` | `[[scan:NAME]]` | scan config name |
| `data_source` | `[[data-source:NAME]]` | name of a data source the project uses |
| `user` | `[[user:<id>]]` | user id; a mention, shown as `@Name` |

Any link takes an optional `|label`. Links by id survive renames and moves.
A link by name breaks when the target is renamed; relink it to one of the
`suggestions`. A `PUT` turns a hand-written `[[doc:path/to/note.md]]` into the
id form when the path is a note the caller can read. A `[[doc:path]]` that is
still in the note comes back `broken` with the reason `path_form`. If the
caller can read a note at that path, `suggestions` holds its id and `label`
its title: save the note to link it by id. A `[[doc:<id>]]` link to a note
the caller cannot read comes back `unavailable`, with no reason, title, path
or route. A link to a deleted note gets exactly the same answer, so a link
never shows whether a hidden note exists. Saving a note with a new
`[[user:<id>]]` notifies that person once, if they are an organization member
who can read the note and is a member of this project. Imports never notify.

`GET /docs/backlinks?kind=&name=` works for every kind: pass the name for a
by-name kind and the id for `doc`, `alert_rule` and `user`.

### Link suggestions {#docs-link-suggestions}

`GET /docs/link-suggestions?q=&kind=&limit=` is what the editor's `[[` and `@`
pickers call. `q` is the typed text, `kind` (optional) narrows to one kind
from the table above, and `limit` caps the rows. The answer is
`{"items": [...]}`, and each item has:

- `kind`: one of the kinds above.
- `id`: the target's id (always present).
- `label`: the name to show.
- `detail`: a second line, such as a path or an email address, or `""` when
  there is none.
- `insert`: the canonical link to write into the note, such as
  `[[metric:signup_rate]]` or `[[user:<id>]]`.

Plan entities come from the plan search. Notes are limited to the ones the
caller can read, people to members of the organization, and alert rules,
branches, scans and data sources to the project. The route allows 240
requests per minute for each user and answers `429` with a `Retry-After`
header after that.

To write, send the whole content:

```http
PUT /api/v1/projects/{slug}/docs/file?scope=project&path=guides/warehouse.md
Content-Type: application/json

{
  "content": "---\ntitle: Warehouse gotchas\naudience: agent\n---\nJoin on [[event:purchase]] carefully.\n",
  "base_revision": 3,
  "message": "Explain the double count"
}
```

The same `PUT` creates or updates. The response is the note plus `created`,
`changed` (`false` when the content was already identical, in which case no
revision is written) and `warnings`, one sentence per broken or ambiguous
link. A broken link does not block the save.

- `base_revision`: send the `revision` you read. If the note changed since,
  the answer is `409` `"Doc changed since revision N"`. Read it again and
  merge. Omit it to overwrite.
- `create_only: true`: `409` if a note already exists at the path.
- A path that differs only in case from an existing note is `409`.
- Content over 256 KiB is `413`. A bad path, invalid frontmatter, or a root
  that already holds 5000 notes is `422`, with the reason in `detail`.

### Note sharing {#docs-sharing}

A note is readable by everyone at its level (`visibility: "level"`, the
default), by its author and the people and groups it is shared with
(`"restricted"`), or by its author only (`"private"`). See
[Sharing](../use/docs-catalog.md#sharing). The rules apply to every docs
route, and to API keys as to their user:

- A note the caller cannot read is left out of `GET /docs`, search, backlinks,
  revisions and the export, and is not counted anywhere. Reading it by path is
  `404` `"Doc not found"`, the same answer as for a missing note.
- An organization owner or admin can read such a note by path
  (`GET /docs/file`, `GET /docs/revisions`, `GET /docs/file/sharing`). The
  answer is `200` with `"break_glass": true` on the note, each read is audited
  as `doc.break_glass_read`, the note is still missing from every list, and
  it stays read-only for them unless it is shared with them for editing.
- A hidden note still holds its path: a create, move or import (also a dry
  run) onto that path is `409` `"A doc already exists at ..."` (or `"the path
  is taken"` in an import report), and the per-scope note limit counts hidden
  notes. None of these names the note or shows its content.
- Each note in a response carries `visibility`, `my_permission` (`"view"` or
  `"edit"`: what the caller may do with it) and `shared` (whether it has any
  share). A `PUT /docs/file` on a note whose `my_permission` is `"view"` is
  `403`.

`GET /docs/file/sharing` returns the note's setting:

```json
{
  "visibility": "restricted",
  "inherited": false,
  "inherited_from": null,
  "shares": [
    {"principal_type": "user", "principal_id": "<user id>", "name": "Alice Example", "permission": "edit"},
    {"principal_type": "group", "principal_id": "<group id>", "name": "Analysts", "permission": "view"}
  ]
}
```

`inherited: true` means the note follows the nearest folder setting above it,
named by `inherited_from` (`null` when no folder sets one). `PUT` takes the
same body without the `name` fields. Send `inherited: true` to follow the
folder again. Groups are organization groups. A share to someone outside the
project (or organization) grants nothing. Only the note's author (while the
level lets them write) or an organization owner or admin may change a note's
sharing (`403` otherwise); an `edit` share lets a caller edit the note, not
re-share it. Each change is audited as `doc.share_update`, with the setting
before and after.

`GET` and `PUT /docs/folder/sharing?path=<folder>` read and set a folder's
setting, which the notes under it that follow their folder use. For a folder
of project notes, `PUT` is allowed to an organization owner or admin, or to a
project editor when every note that follows the folder is their own or still
has `visibility: "level"`; for organization notes, to an organization owner
or admin. A folder's `"private"` means each note's own author, not the caller
who set it.

Frontmatter never carries visibility. An import ignores it, so imported notes
get the default or their folder's setting, and an export does not write it.

`POST /docs/move` takes `{"scope", "from_path", "to_path", "folder"}`. With
`"folder": true` both paths are folder prefixes and every note under
`from_path` moves. The move is all or nothing: if any target path is taken,
the answer is `409` and names them. A move never changes who can read a
note behind its author's back: a single note that follows its folder takes
its new folder's setting only when the caller is its author or an
organization owner or admin (audited as `doc.share_update` with
`"via_move": true`). Otherwise, and for every folder move, a moved note whose
access would change keeps its old setting as its own
(`inherited: false`), and the `doc.move` audit row lists it under
`access_kept`. A folder move carries its folder settings only onto a target
folder that holds no other notes and has no setting. `DELETE /docs/folder` returns
`{"deleted": [...]}` and is `404` for a folder with no notes.

`GET /docs/revisions` lists revisions newest first. `GET
/docs/revisions/{id}` adds the revision's `content` and a unified `diff`
against the revision before it (`""` for the first, capped at 200 KiB with
`diff_truncated`). `POST .../restore` (body `{"message": ""}`) writes the old
content as a new `restore` revision and returns the same shape as `PUT`.

`GET /docs/search` searches notes only, through the same index as
`/search?types=doc`, and returns `scope`, `path`, `title`, `description`,
`tags`, `audience`, `snippet`, `score` and `confidence` per hit. In
`/search`, a note's `route_path` is `/p/{slug}/docs/{scope}/{path}` and its
`subtitle` is `Project notes · {path}` or `Organization notes · {path}`.

Export and import use a bundle:

```json
{
  "format": "tripl-docs/v1",
  "scope": "project",
  "project_slug": "shop",
  "organization_slug": "default",
  "exported_at": "2026-09-27T10:00:00Z",
  "files": [{"path": "SKILL.md", "content": "---\nname: event-query-recipes\n---\n…", "sha256": "…"}]
}
```

`POST /docs/import` takes `{"format": "tripl-docs/v1", "files": [...]}` (at
most 2000 files and 20 MiB). `sha256` is optional and checked when present.
`mode=merge` creates and updates; `mode=mirror` also deletes notes the bundle
does not carry. The result lists `created`, `updated`, `unchanged`, `deleted`,
`skipped` (non-`.md` files, with a reason) and `errors`. With
`dry_run=true` nothing changes and errors are reported in the result. Without
it, any error aborts the import with `422` and `{"detail": {"errors": [...]}}`.
`POST /docs/import/zip` takes the same query parameters plus `keep_root`, and
a multipart field `file` (a zip of at most 10 MiB). A single top-level folder
shared by every entry is removed unless `keep_root=true`. A zip export
(`format=zip`) holds one entry per note at its path, with no wrapper folder.
See [the agent-skill example](../use/docs-catalog.md#example-an-agent-skill)
for how a `SKILL.md` + `references/` folder maps onto note paths.

The MCP server exposes `list_docs`, `read_doc`, `search_docs` and `write_doc`,
and the CLI has `tripl docs ls|cat|pull|push`.

## Safe Agent Defaults

- Use a project-scoped `read` key for retrieval agents.
- Use a project-scoped `write` key only for agents that are explicitly allowed to edit the tracking plan.
- Pass `branch=<branch_id>` for all write calls unless the operator intentionally wants to edit main.
- Search first, then fetch the canonical entity by id before making decisions.
- Check the docs catalog (`search_docs` / `GET /docs/search`) for the team's notes on the events you work with, and send `base_revision` when you write a note.
- Before a delete, deprecate or rename, check `GET /projects/{slug}/dependencies` (or `POST /impact` for several changes) and report what it names; the write itself will not stop you.
- Before you add tracking code for an event, or after you change it, validate the calls with `POST /projects/{slug}/plan/validate`. It needs only a `read` key.
- Prefer partial `PATCH` payloads over sending whole objects.
- Treat field and meta value lists as full replacements when included in an event update.
- Monitoring outputs — signals, schema/distribution/variable-value drift, and
  app-version **release regressions** — are scan-produced. Query them through
  the endpoints in `/openapi.json`; only their explicit review/action endpoints
  mutate resolution state.
- Keep `/openapi.json` in the agent's tool context and use this guide for tripl-specific auth, branch, and workflow rules.

## Interactive API reference

Every endpoint — with request/response schemas — is rendered from the live
OpenAPI spec at **[API Reference](/integrate/api)** (also linked as **API** in the
top navigation). Regenerate the underlying spec with `bin/dump-openapi.sh` after
changing the HTTP API.
