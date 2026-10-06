---
title: Plan governance
sidebar_position: 2.7
---

# Plan governance

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md). Each
project's own merge policy (minimum approvals, blocking self-approval), event
type owners, reviewers, required fields and contracts, and
[`tripl check`](../integrate/tripl-check.md) are part of Community.
:::

A **plan policy** holds rules that every project of the organization, or the
projects it names, must follow on top of each project's own merge policy:

- **naming rules**: a pattern that event, event type, field or variable names
  must match;
- **required fields**: fields every event type must define;
- **forbidden names**: names no field, variable or tracked property may use;
- **approval of sensitive fields**: a branch that adds a field marked
  sensitive, or changes a field's sensitivity, needs an approval from a member
  of a group you choose;
- **protected main**: the main plan takes changes only through a merged plan
  branch.

tripl checks the rules at three points: when code or payloads are
validated ([`tripl check`](../integrate/tripl-check.md) and `POST
/plan/validate`), when a branch is merged, and when someone edits main
directly.

Open **Settings → Organization → Plan governance**. Owners and admins of the
organization manage policies. **Settings → Project → Plan rules** in each
project links here.

## Policies {#policies}

A policy has a name, an **On** switch, and a scope: **All projects**, or the
projects you pick. A policy that is off is kept but checks nothing. An
organization can have several policies; every policy that is on and covers a
project applies to it, and a violation names the policy it comes from.

Start a policy from scratch or from a preset:

| Preset | What it sets |
|---|---|
| **Snake case names** | Event types, fields and variables in `snake_case`; event names in `snake_case`, with `:` or `.` between parts. |
| **Privacy guard** | Forbids field and property names such as `email`, `phone_number`, `ssn`, `ip_address`, and turns on approval of sensitive fields. Pick the approver group before you save it. |
| **Protected main** | Turns on protected main. |

A preset only fills in the editor; change anything before you save.

### Naming rules {#naming}

A naming rule is a regular expression (Python syntax) per kind of name: event
names, event type names, field and property names, variable names. The **whole**
name must match it: `[a-z_]+` accepts `checkout_started` and refuses
`CheckoutStarted`. Leave a kind blank to accept any name. A pattern is at most
300 characters, and one that does not compile is refused when you save.

### Required and forbidden names {#fields}

**Required fields** are field names every event type must define, such as
`platform`. **Forbidden names** may not be used by a field, a variable or a
property a tracking call sends. Both lists compare names in any case, and a name
cannot be both required and forbidden.

### Sensitive fields {#sensitive}

A field or meta field has a **sensitivity**: none, PII, PHI, financial or
secret. With **Approve sensitive fields** on, a branch that adds a field with a
sensitivity other than none, or changes a field's sensitivity, cannot be merged
until a member of the **approver group** has approved the branch's current
content. The approval works like any other branch approval: it is tied to the
reviewed content, so an edit after it makes it stale. The branch's author does
not count, even when they are in the group. The refusal lists the members who
can approve.

If the approver group is deleted, merges that change a sensitive field are
refused until an owner or admin picks another group.

### Protected main {#protected-main}

With **Protected main** on, every change to the main plan through the API or the
app is refused with `409`: create the change on a [plan
branch](../use/user-guide.md), get it approved and merge it. Merging is how
changes reach main. Reading main is never affected.

## Where the rules are checked {#enforcement}

| Where | What is checked | What happens |
|---|---|---|
| `tripl check`, `POST /projects/{slug}/plan/validate` | Each call: an event name the plan does not know against the event naming rule, every key it sends against the forbidden names and the field naming rule, and, for a whole payload (`complete: true`), the required fields. | A `policy_violation` finding on the call, with `rule`. An error fails `tripl check`. |
| Merging a branch | The branch's plan against its merge base: names, required fields and forbidden names on what the branch adds or renames, and approval of sensitive fields. | `409` with `policy_violations`. The branch's page shows the message. |
| Editing main | Protected main. | `409` with `policy_violations`. |

Only what a branch **newly** breaks blocks its merge. A name on main from before
a rule existed is not reported against an unrelated branch, so turning a policy
on does not stop every merge until main is cleaned up. A planned event that
breaks a naming rule is reported where the plan changes, at the merge, not on
each call that sends it.

The policies run after the project's own merge gates (approvals, owners), which
are unchanged.

### Rule names {#rules}

Each violation carries a `rule`:

| `rule` | Meaning |
|---|---|
| `naming.event`, `naming.event_type`, `naming.field`, `naming.variable` | A name does not match the naming rule for its kind. |
| `required_field` | An event type, or a whole payload, lacks a required field. |
| `forbidden_field` | A field, variable or property uses a forbidden name. |
| `sensitive_approval` | A sensitive field was added or reclassified without an approval from the approver group. |
| `protected_main` | A direct edit of a protected main. |

A refused merge or edit answers:

```json
{
  "detail": {
    "message": "Blocked by 1 plan rule: Field 'track.Email' uses the name 'Email', which policy 'Privacy guard' forbids.",
    "policy_violations": [
      {
        "rule": "forbidden_field",
        "message": "Field 'track.Email' uses the name 'Email', which policy 'Privacy guard' forbids.",
        "severity": "error",
        "entity_type": "field",
        "entity": "track.Email",
        "field": "Email",
        "approver_ids": []
      }
    ]
  }
}
```

A refused edit of main has the message in `detail` and `policy_violations` beside
it.

## API {#api}

All routes are under `/api/v1/orgs/{org}/governance`, for owners and admins of
that organization from a browser session. A member and an API key get `403`;
someone outside the organization gets `404`.

| Route | What it does |
|---|---|
| `GET /options` | The organization's projects and groups, and the presets. |
| `GET /policies` | Every policy. |
| `POST /policies` | Create a policy (`201`). |
| `GET /policies/{policy_id}` | One policy. |
| `PUT /policies/{policy_id}` | Replace a policy. |
| `DELETE /policies/{policy_id}` | Delete a policy (`204`). |

A policy body:

| Field | Type | Meaning |
|---|---|---|
| `name` | string, 1 to 120 | Unique in the organization (`409` otherwise). |
| `description` | string | Optional. |
| `enabled` | boolean | Default `true`. |
| `all_projects` | boolean | Default `true`. |
| `project_ids` | list of ids | The projects, when `all_projects` is `false`. At least one. |
| `naming_rules` | object | `event`, `event_type`, `field`, `variable`: a pattern each, optional. |
| `required_fields` | list of names | Up to 100. |
| `forbidden_fields` | list of names | Up to 100. |
| `sensitive_approval` | boolean | Default `false`. |
| `sensitive_approval_group_id` | id | Required when `sensitive_approval` is `true`. |
| `protected_main` | boolean | Default `false`. |
| `preset` | string | The preset it started from, informative. |

A project or group of another organization, or a pattern that does not compile,
is `422`.

## Audit {#audit}

Every change is in the organization's [audit log](./audit.md), with the policy
before and after: `org.governance_policy.create`,
`org.governance_policy.update` and `org.governance_policy.delete`.
