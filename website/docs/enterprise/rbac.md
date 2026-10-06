---
title: Access control
sidebar_position: 2.7
---

# Access control

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md).
Organization roles, project roles, the default access to projects and
organization groups are part of Community; see
[Roles & permissions](../administer/admin-guide.md#roles--permissions).
:::

Community decides access with fixed roles: an organization's owners and admins
hold every project, and everyone else holds the role their project membership
or the organization's default gives them (`editor`, `viewer` or none). The
Enterprise edition adds three things on top:

- **group roles in projects**: give an organization group a role in a project,
  and every member of the group holds it;
- **custom roles**: an editor who may change only some things, such as the docs
  or the alert settings;
- **team sync**: at each single sign-on, put people in the organization groups
  that match their groups at the identity provider, and take them out of the
  ones that no longer match.

Open **Settings → Organization → Access control**.

## Group roles in projects {#grants}

A grant gives one organization group one role in one project of the same
organization: **Editor**, **Viewer**, or a [custom role](#custom-roles). Every
member of the group holds that role in the project from that moment on.

- **The higher role wins.** A grant adds to the member's own role in the
  project (their membership row, or the organization's default). An editor who
  is also in a group with **Viewer** stays an editor; a member with no access
  who is in a group with **Viewer** can now read the project. A membership row
  of **No access** does not block a grant: to keep someone out, take them out
  of the group.
- **A grant never makes anyone an owner** and never changes an organization
  role. Organization administration (members, data sources, the audit log,
  deleting projects) stays with owners and admins.
- **It ends at once.** Taking someone out of the group, removing the grant,
  deleting the group or removing the person from the organization all take the
  role away on their next request; an open live stream of the project closes.
- **It stays in its organization.** A group of one organization can never hold
  a role in another organization's project, and only current members of the
  project's organization ever hold a grant.
- A group holds one role per project. To change it, change the grant.

## Custom roles {#custom-roles}

A custom role is an editor limited to the permissions you pick. Give a group a
custom role in a project, and its members can read everything there but change
only what the role allows. The permissions:

| Permission | What it covers |
|---|---|
| Edit the tracking plan (`plan.edit`) | Events, event types, fields, properties, relations, planned events, branches and their reviews, reconciliation |
| Merge branches (`plan.merge`) | Merging a branch into the plan, reverting a merge |
| Comment (`comments.write`) | Comments on events, screenshots and branches |
| Edit docs (`docs.edit`) | The project's notes, folders, translations and sharing |
| Manage metrics (`metrics.manage`) | The metrics catalog, its previews, chart annotations |
| Manage alerts (`alerts.manage`) | Alert destinations and rules, the alert inbox, monitors, anomaly signals and settings |
| Run data scans (`data_sources.manage`) | Running or cancelling a scan, the project's fact tables, and reading a warehouse's tables and columns through the project |
| Manage the project (`settings.manage`) | The project's own settings, its members, the search index |

How permissions combine:

- someone whose **only** way to edit a project is one or more custom roles may
  use the permissions of all of them together;
- anyone who can edit the project any other way (an `editor` membership row,
  the organization's default, a group with **Editor**) keeps every permission;
- owners and admins of the organization always hold every permission; a viewer
  holds none.

A write a custom role does not allow is refused with `403` "Your role in this
project does not allow this change". Reading is never restricted: a custom role
narrows what someone may change, not what they may see in the project. The
one read it does narrow is a warehouse's catalog (its tables and columns),
which only someone who may run data scans in the project gets through it. A custom role that a
grant uses cannot be deleted; change or remove those grants first.

## Team sync {#team-sync}

Team sync keeps organization groups in step with the groups your identity
provider sends at sign-in. It needs [single sign-on](./sso-and-scim.md) for the
organization.

1. Name the **groups claim**: the OpenID Connect claim (often `groups`) or the
   SAML attribute (for example `memberOf`) that lists the person's groups. A
   claim may hold one string or a list of strings.
2. **Map** each identity provider group (its value, exactly as the provider
   sends it) to an organization group. Several values may map to the same
   group.
3. Turn on **Sync groups at sign-in**, and save.

At each sign-in through the organization's identity provider:

- the person joins every mapped group whose value the claim lists, and leaves
  every mapped group whose values it does not list. Their roles from those
  groups' grants follow at once;
- groups that no mapping names are never touched, so groups you manage by hand
  keep working;
- **no claim means no groups.** A sign-in that carries no groups claim, or one
  that is not a string or a list of strings, takes the person out of every
  mapped group. Losing access when the provider stops sending the claim is the
  safe failure; check your provider's claim settings (some providers leave the
  claim out when a person is in very many groups);
- with **Create missing groups**, a value that no mapping names gets a new
  organization group of that name, mapped to it. An existing group of that
  name is never taken over: map it yourself if you mean it. At most 50 groups
  are created per sign-in.

The first sign-in that links an existing account (the confirmation step) syncs
at that person's next sign-in. A group that [SCIM](./sso-and-scim.md#scim)
manages cannot be mapped: its provider already decides its members. Nor can
the SCIM admin group: its members are organization admins, and team sync never
changes an organization role (a group team sync maps cannot become the admin
group either).

## Who can see and change it

| | Owners | Admins | Members | API keys |
|---|---|---|---|---|
| Read grants, custom roles and team sync | yes | yes | no | no |
| Change them | yes | no | no | no |

Both need a browser session; any API key is refused with `403`. A user who is
not in the organization gets `404`.

Every change is recorded in the organization's audit log:
`org.project_role.create`, `.update` and `.delete`;
`org.group_grant.create`, `.update` and `.delete`; `org.team_sync.update`
with the settings before and after; and `org.team_sync.apply` for each sign-in
that changed someone's groups, with the groups joined, left and created.

## API

All under `/api/v1/orgs/{org}/rbac`, from a browser session: reads for owners
and admins, changes for owners.

| Method | Path | What |
|---|---|---|
| `GET` | `/permissions` | The permissions a custom role picks from |
| `GET` | `/roles` | The custom roles, with how many grants use each |
| `POST` | `/roles` | Create a custom role (`201`); `409` for a name in use |
| `PUT` | `/roles/{role_id}` | Replace a custom role |
| `DELETE` | `/roles/{role_id}` | Delete a custom role (`204`); `409` while a grant uses it |
| `GET` | `/grants` | Every group's roles in projects |
| `POST` | `/grants` | Give a group a role in a project (`201`); `409` when it has one there |
| `PUT` | `/grants/{grant_id}` | Change a grant's role |
| `DELETE` | `/grants/{grant_id}` | Remove a grant (`204`) |
| `GET` | `/team-sync` | The team sync settings and mappings |
| `PUT` | `/team-sync` | Replace them, mappings included |

A custom role body:

```json
{"name": "Docs writers", "description": "", "permissions": ["docs.edit", "comments.write"]}
```

A grant body (`custom_role_id` only with `"role": "custom"`):

```json
{"group_id": "…", "project_id": "…", "role": "custom", "custom_role_id": "…"}
```

A team sync body:

```json
{
  "enabled": true,
  "claim": "groups",
  "create_missing": false,
  "mappings": [{"idp_group": "eng-analytics", "group_id": "…"}]
}
```

A group, project or custom role that is not the organization's answers `404`;
an unknown permission, a `custom_role_id` without `"role": "custom"` (or the
reverse), a value mapped twice and unknown fields are refused with `422`.
