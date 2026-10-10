---
title: Platform console
sidebar_position: 3
---

# Platform console

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md).
:::

## Overview {#platform-console}

The **platform console** is where the operator of a hosted instance looks after
the organizations and accounts on it. It is for **platform admins** only and
lives in **Settings → Platform**, as two pages beside the platform settings:
**Organizations** (`/settings/platform/orgs`, one organization at
`/settings/platform/orgs/{org}`) and **User accounts**
(`/settings/platform/users`). Both are shown only when your account is a
platform admin. The short address `/platform` redirects to **Organizations**
(and `/platform/users` to **User accounts**). Its API is under
`/api/v1/platform` and, like the platform settings, takes a platform admin's
browser session and never an API key (`403 Platform admin session required`).
The endpoints are listed in the
[Agent API guide](#the-api).

The console shows **metadata and counts, never project content**: an
organization's name, slug, status, creation date, member and project counts,
owners' emails, and on its detail page the member list (email, name,
organization role) and the project list (slug, name, creation date). Reading an
organization's plan, scans or alerts takes a
[read-only step-in](#read-only-step-in), which that organization can see in its
audit log.

### Organizations {#platform-organizations}

**Settings → Platform → Organizations** lists every organization on the instance with a search box
(name or slug) and a status filter (`active`, `suspended`, `deleting`), each row
with its member and project counts and its owners. Opening a row shows the
organization's members and projects.

#### Suspend an organization

**Suspend** (with a reason, required, up to 500 characters) stops an
organization without deleting anything:

- Every request that acts in the organization answers
  `403 This organization is suspended` — reads and writes, from its members'
  browser sessions and from its API keys, on `/api/v1/orgs/{org}/...` and on the
  short paths alike. Open live-update streams in its projects are closed too.
  The one exception is a platform admin's
  [read-only step-in](#read-only-step-in), which may still read a suspended
  organization to investigate it.
- The organization stays in its members' organization list
  (`GET /api/v1/orgs`) with `status: "suspended"`, and opening it in the app
  shows a full-page **This organization is suspended** notice instead of the
  workspace, so members know why it stopped rather than finding it gone.
- Scheduled work stops for its projects: scans and metrics collection, alert
  evaluation and digests, escalations, notification digests, sunset alerts and the
  search-embedding sweeps all skip projects of an organization that is not
  active. The same rule keeps them off an organization that is being deleted.
- Nothing is removed. **Unsuspend** makes it active again, and the schedules
  pick its projects up on their next run.

A suspended organization is still listed in the console, and it cannot be
deleted until it is unsuspended. Suspending or unsuspending an organization that
is being deleted is refused (`409`), and the default organization can never be
suspended (`409`), whatever the `DEPLOYMENT_MODE`.
Both actions are audited in the organization itself (`org.suspend`, with the
reason, and `org.unsuspend`), with the platform admin as the actor, so its
owners see who did it and why.

### Users {#platform-users}

**Settings → Platform → User accounts** lists every account with a search box (email or name): whether it is
a platform admin, whether its address is verified, when it was created and how
many organizations it belongs to. **Grant platform admin** and **Revoke platform
admin** each ask for confirmation. Two revocations are refused with `409`:

- the **last** platform admin — an instance always keeps one;
- **your own** flag — another platform admin has to revoke it, so nobody locks
  themselves out by accident.

Grants and revocations are audited as `platform.admin_grant` and
`platform.admin_revoke`. They belong to no organization, so no organization's
audit feed lists them; reading them takes a direct database query, like
`org.delete_complete`.

The first platform admin of a hosted instance, or a replacement when every
platform admin has lost access, comes from the
[`tripl-admin`](../run/configuration.md#tripl-admin) command on the server,
not from the console. It also marks the account's address verified if it was
not, since whoever runs it controls the instance.

### Read-only step-in

A platform admin is not a member of the organizations on the instance and sees
none of their projects (`404`). To look into one — to answer a support question
or check a report of abuse — the admin **steps in**: **Step in (read-only)** on
the organization's row or its detail page asks for a reason and a duration,
then opens the organization at `/o/{org}`. An active or a **suspended**
organization can be stepped into (suspension blocks its members and keys, not
an investigation); one that is being deleted cannot.

The rules:

- **Read-only, always.** For the step-in's lifetime the admin acts in the
  organization as a `member` who is a `viewer` of every one of its projects.
  Every request that is not a `GET`, `HEAD` or `OPTIONS` is refused with
  `403 Step-in is read-only`, with exactly two exceptions, the two read-shaped
  `POST` queries the charts use:
  `POST /api/v1/projects/{slug}/anomalies/signals/query` and
  `POST /api/v1/projects/{slug}/events/window-metrics`. Organization settings,
  members and roles, invitations, API keys and deleting the organization stay
  out of reach, and so does everything that needs the organization's owner or
  admin role (data-source connection details, scan SQL, the audit log).
- **A reason is mandatory** (1 to 500 characters), and it is recorded.
- **It expires.** The duration is 5 to 240 minutes (60 by default). After that
  the admin is back to `404`. **End now** in the banner, or
  `POST /api/v1/platform/step-ins/{id}/end`, ends it early.
- **One at a time per organization.** Stepping in again to the same
  organization supersedes the earlier step-in: it ends, and the new one (with
  its own reason and duration) takes over.
- **Browser session only.** A step-in never extends to an API key, the admin's
  own keys included.
- **Audited in the organization.** Starting writes `platform.step_in` (with the
  reason, the duration and the expiry) and ending writes
  `platform.step_in_end` to the organization's own audit log, with the platform
  admin as the actor, so its owners and admins see every step-in in
  **Audit**. A step-in that simply runs out is recorded as
  `platform.step_in_end` with `expired: true`, written lazily: when the
  instance next looks at that step-in (the admin's next request, or a listing
  of step-ins), not at the exact expiry time.

While a step-in is active, every page of that organization shows a banner:
**Read-only step-in to *Acme* — ends at 14:30 — End now**. A write the admin
tries anyway surfaces the backend's `403 Step-in is read-only`.
`GET /api/v1/auth/me` lists the caller's active step-ins
(`active_step_ins: [{org_slug, expires_at}]`), which is what the banner reads.

## Security


The [platform console](#platform-console)
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

## The API


The instance operator's API, under `/api/v1/platform`. Every route takes a
**platform admin's browser session**: an API key is `403 Platform admin session
required` whatever its scope or owner, and anyone else is `403 Platform admin
required`. Agents cannot use it; it is listed so a client can tell it apart.
Organizations are named by slug; nothing here returns project content. See
[Platform console](#platform-console) for the
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
