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

- **Organization** — Details, Members, Invitations, Data sources and API keys of
  the organization you are working in, then your own Profile and Security, and
  the **Instance** sections (org owners and admins, and the platform admin for
  the operator ones).
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

Access is decided by two roles and one flag:

- the **organization role** (`organization_members.role`): `owner`, `admin` or
  `member`. A self-hosted instance has one organization, the default one, and
  every account belongs to it;
- the **project role** of a member (`project_members.role`): `editor` or
  `viewer`, per project;
- the **platform admin** flag (`users.is_platform_admin`): the operator of the
  instance, separate from both.

| Who | Projects they see | Edit plan content | Manage project members | Data sources, scan SQL, audit log, delete projects | Members, roles & invitations | Organization settings (row limits, photo storage and MIME types, AI model and prompts) | Operator settings (security, observability, email, storage, AI endpoint, system) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Org owner** | Every project of the org (as project `owner`) | Every project | Every project | Yes | Yes, including other owners | Yes (default org) | No, unless also platform admin |
| **Org admin** | Every project of the org (as project `owner`) | Every project | Every project | Yes | Yes, except making or unmaking an owner | Yes (default org) | No, unless also platform admin |
| **Member, project `editor`** | That project | That project | Projects they created | No | No | No | No |
| **Member, project `viewer`** | That project | No | No | No | No | No | No |
| **Member, no project row** | — (`404`) | No | No | No | No | No | No |
| **Platform admin** (flag only) | None from the flag; read-only during a [step-in](#read-only-step-in) | No | No | No | No | Yes | Yes |

An owner and an admin differ only on owners: an admin can do everything an owner
can except promote someone to owner, demote an owner, or invite at the `owner`
role (`403 Only an owner can manage owners`). Owner-only matters that come later
(SSO, deleting the organization) will be the owner's.

The platform admin is an **operator** role, not an organization one. The flag
grants the operator settings and nothing inside any organization: a platform
admin who is not a member sees no project (`404`), no audit log and no data
source connection. The one way in is a time-limited, audited
[read-only step-in](#read-only-step-in) from the
[platform console](#platform-console), which also suspends organizations and
grants or revokes the flag. On a self-hosted instance the first account is both
the default organization's owner and the platform admin, so a one-person
instance never notices the split.

How roles are assigned:

- **The first user to register becomes the owner of the default organization
  and the platform admin.** This guarantees every instance has someone who can
  manage roles and operate it. Every later registration joins the default
  organization as a **`member`** with no project, until someone adds them to
  one.
- An invitation carries an organization role; the invitee joins at it.
- A `viewer` is a **project** role now: add someone to a project as a viewer in
  **Settings → Project → Access**. Upgrading moved every former instance viewer
  to `member` and capped each of their project memberships at `viewer`, so
  nobody gained write access in the upgrade.
- Only an **owner or admin** changes organization roles. The API refuses to
  **demote the last remaining owner** of an organization (`400 Cannot demote
  the last remaining owner`), so you cannot lock yourself out.

`PATCH /api/v1/users/{id}` takes the organization vocabulary, `{"role":
"owner" | "admin" | "member"}`, and writes the organization role in the request's
organization. The instance-era values are refused with `422`; the old `owner`
maps to `owner`, and `editor` and `viewer` both map to `member` — what a member
may do in a project is their project role. `users.role`, the old instance role,
is no longer read by anything and will be dropped.

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

:::note A role change does not sign the user out
Roles are read from the database on every request, so a change applies to the
member's next request without ending their sessions. (Before organizations, a
role change deleted every session of the user.) An in-flight request that
already passed authentication still finishes with the old role. To end
someone's sessions, have them log out or remove them from the organization.
:::

### How permissions are enforced

The backend gates endpoints with role/scope dependencies, not just UI hiding:

- **Project membership** is checked before anything else on every project
  route: a non-member receives `404 Project not found`, and a viewer member who
  tries to change something receives `403 Editor access to this project is
  required`.
- **Organization membership** is required to create a project or a write-scoped
  API key, and to read the member roster (`403 Organization membership
  required`).
- **Org owner or admin** is required for data sources, scan authoring, the audit
  log, deleting a project, danger-zone resets, tracker and branch settings,
  members, roles and invitations (`403 Organization owner or admin role
  required`). On a project route the project must belong to the organization the
  request acts in.
- **Organization settings** (`/api/v1/orgs/{org}/settings`: email, AI, search
  embeddings and row limits; `.../settings/trackers`: Jira and Linear defaults)
  take an owner or admin of that organization; the row limits alone are
  readable by every member. A body naming an operator field is refused with
  `422`. **Platform settings** (`/api/v1/platform/settings`) take a platform
  admin (`403 Platform admin required`).
- **Settings admin** — a platform admin, or an owner or admin of the
  organization the request acts in — is required for the older combined
  `/settings`. A write that touches an operator field additionally needs the
  platform admin (`403 Platform admin required`); organization fields land in
  that organization's settings (on a self-hosted instance, the instance's —
  where the SMTP relay and AI endpoint also need the platform admin, through
  either route). Only a platform admin reads the Security, Storage,
  Observability and system blocks of `/settings`.
- Owner- and admin-only endpoints additionally require an **interactive
  session** — an API key does not reach them, even a write-scoped key of an
  owner (`403 Owner session required`). One route is deliberately exempt: the
  [metrics replay](../integrate/agent-api-guide.md#replaying-metrics), which a
  write-scoped key of an org owner or admin may call. The operator settings
  never take a key (`403 Platform admin session required`).

## Project access

Each project has its own member list. A user who is not a member of a project
does not see it at all: it is missing from the project list, the activity feed
and data-source listings, and every page and API route under it answers
`404 Project not found`. Owners and admins of the organization see every
project of it and need no membership.

| Project role | What it allows |
| --- | --- |
| **Editor** | Read the project and edit its tracking plan, catalog and alerting. |
| **Viewer** | Read the project. |

The membership row is authoritative: nothing caps it from outside.

**Settings → Project → Access** lists the project's members. The organization's
owners and admins, and the person who created the project, can, as long as the
creator still holds an editing role:

- **add a member**: pick someone from the organization and a role (a user who
  is not a member of the project's organization is refused with `422`);
- **change a member's role** between Editor and Viewer;
- **remove a member**, who then no longer sees the project. Removal also drops
  their event-type ownerships and pending branch-reviewer assignments in that
  project, and closes any live-updates stream they have open within one
  heartbeat.

The creator's rights last only while they can edit: a creator who was switched
to a `viewer` member gets `403` on member changes, rename and reset, and a
creator who was removed from the project gets `404` like any other non-member.
Deleting a project is for the organization's owners and admins only.

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
- **Taken slugs still answer `409`, within the organization.** Creating a
  project with, or renaming one to, a slug that already exists in the same
  organization is refused with `409` even if the caller cannot see that
  project. Slugs are unique per organization: another organization may use the
  same slug, and its projects never show up in this organization's lists.
- **Some slugs are reserved.** `demo`, `orgs`, `new`, `settings`, `api`, `p`,
  `o` and the other top-level route names cannot be project slugs (`422`).
  Generated demo slugs such as `demo-3f2a1c` are fine. A project created before
  a slug was reserved keeps it and can still be edited; only renaming a project
  to a reserved slug is refused.
- **Links tripl sends name the organization.** Alert messages, digests,
  notifications and their emails, search results, the activity rail, dependency
  and impact links and incident-summary citations all link to
  `/o/{org}/p/{project}/...`, so a link about one organization's `web` opens that
  project even when another organization also has a `web`. Messages read
  outside the app (alerts, emails) carry absolute links built from the
  **App base URL** setting (`app_base_url`).
  - Links written before this change (`/p/{project}/...`) are not rewritten:
    notifications already stored, and alerts already sent to Slack or email,
    keep their old form, and the app redirects them to the organization that
    holds that project (the default organization, or your only organization
    with that slug). Such an old link is ambiguous only for someone who belongs
    to two organizations that both have the slug.
  - Search keeps storing the organization-less path and adds the organization
    when it answers, and incident summaries do not count links as part of what
    makes a summary stale, so this change re-indexes nothing and regenerates no
    AI summary.
  - Screenshot file URLs in the API name the organization too
    (`/api/v1/orgs/{org}/projects/{project}/events/.../photos/.../file`); the
    old `/api/v1/projects/...` form keeps working.
  - Invitation links (`/invite/{token}`) are not project links and are unchanged.
- **Data source names are per organization** too: two organizations may each
  have a source called `warehouse`, and a name clash inside one organization is
  a `409`.
- **Lists and the notification bell are per organization.** `GET /projects`,
  the data-source list and `/me/notifications` show only the organization the
  request acts in; use the `/api/v1/orgs/{org}/...` form of the URL to read
  another organization you belong to. The demo-workspace cap per creator is
  counted per organization.
- **Upgrading to organizations** capped every former instance viewer's project
  memberships at `viewer`, and made every former instance owner an owner of the
  default organization.
- **Upgrading from a version without project access** kept everyone's access:
  every existing editor and viewer became a member of every existing non-demo
  project with the same role, and each existing demo kept only its creator.

## Members

**Settings → Members** lists the members of the organization, with their
organization role. The roster is visible to every member of the organization
(an account outside it gets `403`); **only owners and admins** see the per-row
role dropdown and can change roles. Everyone else sees read-only role chips.

Each row shows the member's name (or email), email, join date, and role
(**Owner** / **Admin** / **Member**). Nobody can change their *own* role from
this screen — use another owner account if you need to step down, and remember
the last-owner guard above. An admin does not get the dropdown on an owner's
row, and is not offered **Owner**.

Granting **Owner** and every demotion (Owner → Admin, anything → Member) ask
for confirmation first and say what changes; a promotion short of Owner
(Member → Admin) applies at once. If a change is refused, the error appears on
that member's row. The member stays signed in.

:::warning Registration ships open — close it once your team has accounts
Self-service registration used to be the only way to add a person, which is why
it ships **open by default**. You can now **invite people directly** instead
(see below), so closing registration no longer blocks onboarding. On an open
instance
anyone who can reach the URL can sign up, join the organization as a
**member**, and immediately read this member roster and the workspace-global
data sources' names. They see no project until someone adds them to one. Data
source connection details (host, port, username) are for org owners and admins
only. Warehouse table and column names are available to members authoring scans
and metrics. For a source owned by a project, the editor must also be allowed to
edit that project; workspace-global sources remain shared. A failed connection
test distinguishes an authentication failure from a network failure, and
renaming a source to an existing name returns a conflict error.
Decide the policy before you expose the instance; see
[Security & access](#security--access) and
[Security & Hardening](../run/security.md#self-service-registration).

### Invite a member

The recommended way to add someone, and the only one that works without opening
the instance to the world:

1. **Settings → Organization → Invitations → Invite a member**. Enter their email and pick an
   organization role (**Member** by default). Picking **Owner** shows what the
   role grants, and creating an owner invite asks for confirmation — whoever
   opens that link administers the organization. Only an owner can invite an
   owner.
2. **Copy the link it returns.** It is shown once and cannot be retrieved
   afterwards — send it however you like. When the instance operator has SMTP
   configured the link is also emailed to the address, always through the
   operator's mail server. The
   panel names the role and stays until you **Dismiss** it; creating another
   invite before the link was copied asks first, since the uncopied link would
   be lost.
3. They open the link, set a password, and land in the organization at the role
   you chose. Someone who **already has an account** (in another organization)
   signs in first and accepts the link: the organization is added to their
   account. That only works for the account whose email the invitation was sent
   to; any other signed-in account is refused. On a
   [hosted](#hosted-sign-up-and-email-verification) instance that account must
   also have verified its address first, or the page shows **Verify your email
   address before accepting an invitation.** A new account made from the link
   is verified at creation on a self-hosted instance. On a hosted instance it
   is not: you received the raw link yourself, so using it proves nothing about
   who reads the mailbox. The new account is emailed a verification link and
   sees **Check your inbox** until it confirms it, like a sign-up. Only a link the server rejects (used, expired or revoked) shows
   **This invite link no longer works**; if the page could not reach the server
   or it failed, it shows **Could not check this invitation** with **Try again**,
   so a network blip does not read as a dead link.

Owners and admins only, and only from a signed-in browser session — an API key
cannot mint an account whatever its scope. The link works a single time, expires after 72
hours, and is bound to the address you typed, so it cannot be redeemed into a
different identity. Pending invitations are listed under **Settings → Organization → Invitations**
and revoking one kills its link immediately. Inviting the same address again
invalidates the previous link.

This works while registration is **Disabled** — that is the point of it.

Adding a member while registration is **Open**: they can also just register
themselves at the sign-in page (they join as a member), and you adjust their
role from **Settings → Members**.

While registration is disabled the sign-in page shows no sign-up form at all,
and `POST /auth/register` is refused with a `403` that tells the visitor to ask
an owner. On a self-hosted instance the one exception is an instance with **no
users at all** — that first registration always works and becomes the owner, so
a fresh or reset deploy can always be claimed. A hosted instance has no such
exception (see [Hosted sign-up and email
verification](#hosted-sign-up-and-email-verification)). Registration is also rate-limited (see
[Security & access](#security--access)).
:::

## Organizations

Every project, data source, API key and invitation belongs to one
**organization**. A self-hosted instance starts with one, the **default
organization** (slug `default`), and everyone who registers joins it. On a
hosted instance each sign-up creates an organization of its own instead (see
[Hosted sign-up and email verification](#hosted-sign-up-and-email-verification)).
An account can belong to several organizations, with a separate role in each.

### Organizations in the app

Every page of the app lives inside an organization, and its address says which
one:

- `/o/{org}` is the organization's workspace — its list of projects;
- `/o/{org}/p/{project}/…` is a project page, for example
  `/o/acme/p/web/events`.

Links written before organizations, `/p/{project}/…`, keep working: they open
the same page in the organization that holds the project — the one of yours whose
projects include that slug; else the default organization, if you are in it; else
your only organization. The query string and `#anchor` are kept. The Settings
pages (`/settings/…`) act in the organization you used last; `/o/{org}/settings/…`
opens them for a given organization.

Someone in more than one organization gets an **organization switcher** above
the project switcher in the sidebar. Picking an organization opens its
workspace. With a single organization the switcher is not shown.

The organization's own settings are in **Settings → Organization**:

- **Details** — the name, which owners and admins can change; the slug, shown
  read-only because it cannot change; **Create organization** (for a platform
  admin on a self-hosted instance, for anyone on a hosted one); and the **Danger zone**, where an owner deletes the organization after
  typing its slug. The default organization has no danger zone: it cannot be
  deleted.
- **Members** — everyone in the organization, with a role select, **Remove**
  (after a confirmation) and, for an owner, **Transfer ownership**.
- **Invitations** — invite someone at an organization role (owner, admin or
  member), see the pending invitations and revoke them. Owners and admins only.
- **Data sources** and **API keys** — the organization's own.
- **Email**, **AI** and **Limits** — the organization's own SMTP relay, AI
  chat provider (endpoint, key, model, timeout, output tokens and the three
  prompts) and default scan/metrics row caps. Owners and admins only. Each
  field shows where its value comes from: **Organization** (set here),
  **Operator** or **Env** (inherited), or **Disabled by operator policy** when
  the operator runs with `ORG_SETTINGS_OPERATOR_FALLBACK=none` and the
  organization has not set its own relay or AI endpoint. Setting any endpoint
  field (SMTP host, AI base URL, model or key) makes that whole group the
  organization's, so the operator's key or password is never sent to the
  organization's server. Limits and AI timeouts cannot exceed the operator's,
  and the SMTP host and AI base URL must be public addresses.
  **Send test email** and **Test AI** probe exactly what the
  organization would use. Alerts, digests, notification emails, AI
  descriptions and answers, alert explanations and incident summaries of the
  organization's projects all use these values; sign-up, password-reset and
  invitation mail always use the operator's relay. See
  [Operator and organization settings](../run/configuration.md#operator-and-organization-settings)
  for the full classification.
- **Search** — the organization's own semantic-search embeddings: the switch,
  provider, model, OpenAI-compatible base URL and API key, with the same source
  badges and **Inherit** actions. The four endpoint fields are one group, like
  the AI endpoint. Saving an own endpoint or model with embeddings on sends one
  test text to it, and the save is refused unless the model answers with
  vectors of the instance's width (1536). A change that moves the
  organization's embedding space queues a reindex of the organization's own
  projects; other organizations are not touched. With
  `ORG_SETTINGS_OPERATOR_FALLBACK=none` and no endpoint of its own, semantic
  search is off for the organization and search is lexical.
- **Photos** — where the organization's event photos go, and what it takes:
  the backend, its own GCS bucket, a service-account JSON key pasted into the
  page (write-only: once saved the page only shows **Configured**), public URLs
  and the signed-URL lifetime — one group, so an own bucket is never written
  with the platform's credentials — plus the upload size cap and the allowed
  content types. The cap cannot exceed the platform's and the content types can
  only be a subset of the platform's list. Without storage of its own the
  organization uses the platform's (the badges say **Operator** or **Env**, and
  the bucket name is not shown). On a hosted instance an organization cannot
  pick the local backend. A new bucket or key applies to the next upload;
  photos already stored keep being read with the bucket and key they were
  written with, so keep the old bucket readable until you have copied them.
- **Trackers** — Jira and Linear defaults for the organization's projects: the
  Jira site (https, public), account e-mail, API token and default project key;
  a Linear API key and default team. A project's own **Tracker** settings
  override each field, and a project still switches ticket automation on
  itself. A project that sets its own Jira site, account or token uses none of
  the organization's three. Tokens are write-only.

The operator's own settings — public URL, security, observability, the storage
server paths (local directory, GCS credentials file), and the operator's
defaults for every organization field (the platform's photo storage, size cap
and content types among them) (including the
embedding switch, provider, model and key; the embedding base URL and
dimensions are env-only) — are under **Settings → Platform**, for platform
admins only. On a self-hosted instance the default organization's Email, AI,
Search and Limits are the same values as the platform's; its owners and admins
can change the limits, timeouts and prompts there, and only a platform admin its
SMTP relay, AI endpoint, embeddings and storage (which stay the platform's
settings and take effect on the next restart). Tracker defaults have no operator layer:
every organization, the default one included, sets its own.

### The organization API

The organization API is under `/api/v1/orgs`; the endpoints are listed in the
[Agent API guide](../integrate/agent-api-guide.md#organizations). Everything
under `/api/v1/orgs/{org}` answers `404 Organization not found` to anyone who is
not a member of that organization — the same answer as for an organization that
does not exist.

### Create an organization

`POST /api/v1/orgs` with a `name` and a `slug`, from a signed-in browser
session. Who may call it depends on `DEPLOYMENT_MODE`:

- **self-hosted** — only a **platform admin**;
- **hosted** — any signed-in user (whose address is verified, like every route
  outside `/api/v1/auth/*` there). Signing up creates the first one.

The creator becomes the new organization's **owner**.

The **slug is permanent**: it is part of every organization-qualified URL
(`/api/v1/orgs/{slug}/...` and the app's `/o/{slug}/p/{project}/...`), and links
already sent in email and Slack must keep working, so they are never rewritten. It follows the project slug rules (lowercase letters, digits and single
hyphens), and names that the app routes as something else (`settings`, `orgs`,
`projects`, `default`, …) are refused.

### Hosted sign-up and email verification

With `DEPLOYMENT_MODE=hosted` (see
[Configuration](../run/configuration.md#organizations)) the instance is a
multi-tenant service, and signing up works differently. A self-hosted instance
behaves as before: nothing below is enforced there, and every account is
marked verified when it is created.

**Sign-up creates an organization.** The **Create account** form also asks for
an **Organization name** and an **Organization URL slug** (derived from the name
until you edit it, with a preview of the `/o/<slug>` address). The API is
`POST /api/v1/auth/register` with `org_name` and `org_slug` besides `email`,
`password` and `name`; on a hosted instance both are required (`422` without
them), and the slug follows the [organization slug
rules](#create-an-organization) (`409` when it is taken). The new account is the
organization's **owner** and joins no other organization, the default one
included. To join an existing organization instead, ask one of its owners or
admins for an [invitation](#invite-a-member). On a self-hosted instance
`org_name` and `org_slug` are ignored and the account joins the default
organization.

**Registration still applies.** `REGISTRATION_MODE=disabled` refuses every
sign-up with `403`. Unlike self-hosted, there is no first-account exception, so
the operator's own account is created the same way as everyone else's.

**The operator's SMTP is required.** Every new address has to be verified, and
account mail always goes through the operator's relay (never an
organization's). Without it, sign-up answers `503 Email delivery is not
configured` before creating anything.

**Addresses are verified.** Right after sign-up the account is sent a link to
`/verify-email`. It works once and expires after 24 hours; asking for a new one
(**Resend email** on the **Check your inbox** screen, or
`POST /api/v1/auth/verify-email/request`) invalidates the earlier unused
links. The link confirms only in a browser signed in as the account it was
sent to: opened while signed out, the page asks you to sign in and then brings
you back to it; opened in another account's session, it is refused like a dead
link and stays unused. Confirming signs the account out everywhere else and
keeps only the session that confirmed. Until the address is verified, the app shows only that screen, and every
API route outside `/api/v1/auth/*` answers `403 Email address not verified`,
whatever the credential. What remains is signing out, reading your own account
(`/auth/me`), resending and confirming the link, and previewing an invitation.

An address also counts as verified when:

- a password reset is completed, because the reset link was mailed to that
  address (a reset also signs the account out everywhere and revokes its API
  keys);
- the account was created on a self-hosted instance: every self-hosted account
  is marked verified at creation, whether by sign-up or invitation;
- the account existed before email verification was introduced.

On a hosted instance an account created from an invitation link is **not**
verified by it: the inviter got the raw link in the API response, so redeeming
it proves nothing. The new account is sent a verification link (a failed send
is logged; **Resend email** sends another) and must confirm it before it can
use the app.

On a self-hosted instance nothing is ever blocked, and
`POST /api/v1/auth/verify-email/request` answers `204` without sending
anything: the check is enforced only when `DEPLOYMENT_MODE=hosted`.

**Signed-in invitation acceptance needs a verified address.** On a hosted
instance an existing account accepts an invitation only after verifying its
address (`403 Verify your email address before accepting an invitation.`); the
rule that the account's email must equal the invitation's still applies.

**Platform admins are granted on verification.** An account whose address is
listed in `PLATFORM_ADMIN_EMAILS` becomes a platform admin only when it
confirms the emailed verification link while signed in as itself. Sign-up,
invitations and password reset never grant it. To bootstrap a hosted instance,
set the list, sign up with a listed address, and open the verification link in
the browser where you are signed in as that account.
Without working mail, grant it on the server instead with
[`tripl-admin grant-platform-admin`](../run/configuration.md#tripl-admin).

`GET /api/v1/auth/status` reports `deployment_mode` and
`email_verification_required` (`true` on a hosted instance), which the sign-in
page uses to show the organization fields. On a hosted instance it always
reports `has_users: true`, so it does not reveal whether the instance is empty.

### Rename an organization

Owners and admins can change the **name** (`PATCH /api/v1/orgs/{org}` with
`{"name": "..."}`). The slug cannot be changed; a request that sends one is
refused with `422`.

### Members and roles

`GET /api/v1/orgs/{org}/members` lists the members with their organization role
(any member may read it). Owners and admins change roles
(`PATCH /api/v1/orgs/{org}/members/{user_id}`) and remove members
(`DELETE /api/v1/orgs/{org}/members/{user_id}`), with the same rules as the
Members screen:

- only an **owner** can make, demote or remove an owner;
- the **last owner** can be neither demoted nor removed.

Removing a member takes away everything the membership carried **in this
organization**: their rows on its projects (with the event-type ownerships and
reviewer seats those carried), every API key of theirs bound to it, which
stops working at once, and every unused invitation into it that they sent or
that is addressed to them, so no link minted earlier can bring them back. Their
account, and their memberships of other organizations, are untouched. They
also leave every [group](#groups) of the organization. The response says how
many project memberships, keys, invitations and group memberships went.

A demotion drops the member's unused invitations at roles they can no longer
grant: an owner who becomes an admin loses their pending `owner` invitations
(the same happens to the caller of a transfer), and an owner or admin who
becomes a member loses their pending `owner` and `admin` ones.

### Groups

A group is a named set of an organization's members, such as *Analysts* or
*On-call*. Groups are the organization's own: another organization never sees
them. Sharing notes with a group, and routing event-type ownership and alerts
to one, build on them in later releases; syncing groups from an identity
provider (SCIM) comes with SCIM provisioning.

Open **Settings › Organization › Groups**. Every member of the organization can
see the groups and who is in each. Owners and admins can also:

- **create** a group with a name and an optional description. Names are unique
  within the organization, ignoring case;
- **rename** it or change its description (**Manage**, then **Save**);
- **add** members, picked from the organization's members, and **remove**
  them;
- **delete** it, after a confirmation. Its members stay in the organization.

Only members of the organization can be in its groups. Removing someone from
the organization removes them from all its groups, and deleting the
organization deletes its groups.

The same actions are available under `/api/v1/orgs/{org}/groups`; see the
[Agent API guide](../integrate/agent-api-guide.md#organizations). Reading works
with an API key of the organization; changes need an owner's or admin's
signed-in browser session.

### Invitations

Invitations are per organization. `POST /api/v1/orgs/{org}/users/invitations`
invites into the organization the path names; the legacy
`/api/v1/users/invitations` keeps inviting into the default organization. See
[Invite a member](#invite-a-member) for the flow. An address that already has an
account may be invited into an organization it is not yet in; inviting a current
member is refused (`409`).

### Transfer ownership

An owner can hand the organization to another member:
`POST /api/v1/orgs/{org}/transfer-ownership` with `{"user_id": "..."}`. The
member becomes an **owner** and the caller steps down to **admin**. Another
owner can always be made with a role change instead; the transfer is the
one-step way to leave the owner seat.

### Delete an organization

Only an **owner** can delete an organization, and never the default one. The
request repeats the slug as a confirmation:

```bash
curl -X DELETE https://tripl.example.com/api/v1/orgs/acme \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"confirm_slug": "acme"}'
```

It answers `202` and the organization is gone for everyone at once: every URL
under `/api/v1/orgs/acme`, its API keys, its invitation links and its members'
organization lists all behave as if it never existed. A background job then
removes everything it owned — each project with its plan, scans, alerting and
photos (including the stored image files), the data sources, API keys,
invitations, organization notes, settings and memberships — and finally the
organization itself. Before that last step it deletes every file left under
the organization's `orgs/{organization id}/events/` prefix, in the platform's
store and in the organization's own bucket. If one of them cannot be deleted
(the store is unreachable, or the bucket's key no longer grants access), the
organization stays in the deleting state and the job runs again later (see
below), so no stored file is left behind with nothing pointing at it. The slug
is free again once the job finishes.

If the job cannot be queued (the message broker is down), the request answers
`503` and the organization is active again. Its audit log shows the
`org.delete_request` followed by an `org.delete_cancel`.

If the job fails for good (it retries a few times), an hourly check queues it
again for any organization that has sat in the deleting state for two hours
without a job working on it, until the purge completes.

Deletion cannot be undone. The audit rows are kept in the database: the
request's rows lose their organization link when the organization row goes, and
the completion (`org.delete_complete`) is filed with no organization. No audit
feed in the app lists any of them after the purge, so reading them takes a
direct database query.

### Audit

Every organization action is audited: `org.create`, `org.rename`,
`org.delete_request`, `org.delete_cancel`, `org.delete_complete`,
`org.member_role_update`,
`org.member_remove`, `org.transfer_ownership`, for groups `org.group.create`,
`org.group.update`, `org.group.delete`, `org.group.member_add` and
`org.group.member_remove`, and for invitations
`user.invite`, `user.invite_revoke` and `user.invite_accept`. They appear under
**Organization** and **Workspace** in the Audit tab's action filter.

The [platform console](#platform-console)'s six actions have a filter group of
their own, **Platform**: `org.suspend`, `org.unsuspend`, `platform.step_in`,
`platform.step_in_end`, `platform.admin_grant` and `platform.admin_revoke`.
Suspension and a platform admin's [read-only step-in](#read-only-step-in) are
recorded in the organization's own log; the two grant actions belong to no
organization (see [Users](#platform-users)).

## Platform console

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
[Agent API guide](../integrate/agent-api-guide.md#platform-console).

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
  evaluation and digests, notification digests, sunset alerts and the
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

## Profile & account security

These two sections live under **Settings → Account** and apply only to the
signed-in user.

### Profile

**Settings → Profile** shows your details pulled from the authenticated account:
**Name**, **Email**, **Role** (your organization role, "Set by a workspace
owner") and the **Timezone**
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
for everyone: a platform admin gets a **Set up email** link beside it, and
everyone else reads "Ask a platform admin to set it up."
Your current password keeps working until you choose a new one from the link.

Changing the password in place, two-factor authentication and a list of
signed-in devices are not built; the page lists them under **Coming later**.
To invalidate a user's sessions today, have them log out (a role change no
longer signs anyone out). Sessions also expire automatically after the
configured TTL (`session_ttl_hours`, default **168 hours / 7 days**).

## API keys & governance

API keys are long-lived bearer tokens for non-browser clients — LLM agents and
CLI scripts. They are managed **per user** at **Settings → API keys** (backed by
`/api/v1/me/api-keys`). Creation and revocation require an interactive session;
a Bearer API key cannot manage keys. A user only ever sees and revokes **their own** keys;
there is no cross-user key administration, even for owners. A key belongs to
the organization it was created in and acts only there; the list shows the keys
of the organization the request acts in.

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
  roles*. A write key writes only where its user holds an editing project role
  (or is an org owner or admin). Creating a write-scoped key requires
  membership of the organization (`403 Organization membership required`).

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
Owner- and admin-only endpoints require an **interactive session**, so an API
key is `403` on them even when its user is an org owner. The single exception is
the [metrics replay](../integrate/agent-api-guide.md#replaying-metrics): it only
re-runs SQL an org owner or admin already authored, so their `tk_w_` key may
trigger one.
In practice that means
**connecting a data source and inviting a member are browser-only steps** — no
CLI, script or agent can do them for you, which is why
[`tripl install`](../run/cli.md#tripl-install) hands you a URL at the end instead
of finishing the job. The [Operator CLI](../run/cli.md) documents which of its
commands need a key at all: `install` and `upgrade` need none, the diagnostics
need `tk_r_`, and three verbs need `tk_w_` behind an editor, admin or owner.
:::

## Instance settings (owners, admins and the platform admin)

**Settings → Instance** is visible to the **settings admins**: the platform
admin, and the owners and admins of the default organization. Until each
organization has its own settings, the instance values are what the default
organization uses, so no other organization's admin may change them. Anyone
else who opens an instance page from a link sees the section's title, a lock
notice ("Owner role is required to view or change instance-level settings. Ask
an owner, or go to Profile.") and a **Go to Profile** link, and the API rejects
them (`GET`/`PATCH`/`PUT /api/v1/settings` all require a settings admin's
browser session). It exposes a curated subset of the server configuration as
overrides stored in the database.

The fields split in two:

| Class | Fields | Who may change them |
| --- | --- | --- |
| **Organization** | Runtime row-limit defaults, and AI enabled, model, timeout, output limit, the three system prompts and the search-embeddings switch | Settings admins |
| **Operator** | Runtime public URL (`app_base_url`), every **Security & access** field (registration mode included), every **Observability** field, every **Email** (SMTP) field, every **Storage** field (this page's Storage section is always the platform's own store; an organization's own storage is under **Settings → Organization → Photos**), the AI base URL and API key, the embedding provider, model and API key, and the **System** section | Platform admin only |

Until each organization has its own values, one value of each field serves
every organization, so a field that routes another organization's data is
operator-only: the SMTP relay carries every user's password-reset and
invitation mail, the photo storage settings decide where every
organization's photos go and whether they are public, and the AI and
embedding endpoints receive every organization's plan text.

An org owner or admin who is not the platform admin does not see the Security,
Email, Observability and System sections; the operator fields of Runtime,
Storage and AI are shown to them disabled. A write that touches any operator
field is refused whole (`403 Platform admin required`). The `system` block of
`GET /api/v1/settings` is `null` for them. The photo and row limits
(`/settings/photo-limits`, `/settings/row-limits`) stay readable by every
signed-in user; they answer with the caller's organization's values, and
`/orgs/{org}/settings/photo-limits` names the organization.

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

Authentication and network policy for everyone on the instance. Platform admin
only: an org owner or admin does not see this section.

- **Registration** (`registration_mode`, default **Open**) — whether strangers
  can create their own account. **Open** allows self-service signup: anyone who
  can reach this instance creates an account, joins the default organization as
  a **member**, and can immediately read the member roster; they see no project
  until someone adds them to one. (On a hosted instance a sign-up creates an
  organization of its own instead; see [Hosted sign-up and email
  verification](#hosted-sign-up-and-email-verification).) Each workspace-global data source's name, type
  and health are visible to them; its connection details (host, port, username,
  whether a password is set) are for org owners and admins only, and the
  password itself is never returned to anybody.
  **Disabled** refuses `POST /auth/register` with a `403` and hides
  the sign-up form on the sign-in page. It defaults to Open for historical
  reasons; an owner or admin can [invite](#invite-a-member) people into a closed
  instance, so close it once your team has accounts. See
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

These are the platform's own store and ceilings: organizations without storage
of their own use it (their files under `orgs/{organization id}/`), the size cap
is every organization's maximum, and the MIME list is the most any organization
may allow. An organization's own bucket is set under **Settings → Organization
→ Photos**.

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
