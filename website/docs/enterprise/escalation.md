---
title: Alert escalation
sidebar_position: 2.5
---

# Alert escalation

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md). Alert
destinations, rules and the [Inbox](../use/alerting.md#the-inbox) are part of
Community.
:::

An alert that nobody acknowledges can escalate. An **escalation policy** lists
steps: once an incident has been open and unacknowledged for a number of
minutes, tripl notifies the next alert destination, member or group. **Alert
routes** decide which policy an incident gets, across all of the organization's
projects. Acknowledging, resolving or muting the incident in the Inbox stops the
escalation.

Open **Settings → Organization → Escalation**.

## Escalation policies {#policies}

A policy has a name, up to **10 steps** and a repeat count.

- **A step** is a number of minutes and a target. The minutes count from the
  moment the incident opened (its first delivery), not from the previous step:
  steps at 5, 15 and 30 minutes notify at 5, 15 and 30 minutes. Steps are in
  order of their minutes; two steps may share the same minute. The longest delay
  is 7 days (10080 minutes).
- **The target** is one of:
  - an **alert destination** of any project in the organization (Slack,
    Telegram, a webhook, email, Jira, Linear, PagerDuty, Microsoft Teams). The
    message goes out through that destination's own channel. A disabled
    destination is skipped;
  - a **member** of the organization, emailed;
  - an organization **group**, each of its members emailed.

  Members and groups are emailed through the organization's email relay, from
  its default From address, the way [owners are
  notified](../use/alerting.md#owner-notifications). Without a relay these steps
  are recorded as skipped, with the reason.
- **Repeat** (0 to 5) runs the whole list again that many more times. The next
  pass starts after the last step: with steps at 5 and 15 minutes and one
  repeat, tripl notifies at 5, 15, 20 and 30 minutes. A policy that repeats
  needs its first step at 1 minute or later.
- **Default policy.** At most one policy is the organization's default; it
  applies to every incident that no route picks a policy for. Making a policy
  the default takes that from any other.

A policy that a route uses cannot be deleted; change or delete the route first.

## Alert routes {#routes}

A route picks a policy for the incidents it matches. Its conditions, each
optional:

- **Projects**: one or more projects of the organization;
- **Alert rules**: one or more alert rules (API);
- **Kind of signal**: the scope that fired (`event`, `event_type`, `metric`,
  `schema`, …);
- **Direction**: spikes or drops.

A condition left empty matches anything, so a route with no conditions matches
every incident. Routes are tried in ascending **order** (ties: the oldest
first); the first **enabled** route that matches wins. An incident no route
matches takes the default policy. Without a default policy and a matching
route, an incident does not escalate.

## When an escalation starts and stops {#lifecycle}

Once a minute, tripl looks for open incidents of organizations that have an
escalation policy:

- **Start.** An incident delivered in the last 24 hours that is open in the
  Inbox and is not already escalating gets an escalation, with the policy its
  routes pick. Its clock starts at its first delivery. Demo projects never
  escalate.
- **Steps.** Each step is sent once, when its minute comes. A step that was
  already more than 10 minutes overdue when the escalation started is not sent:
  adding a policy does not page anyone about an incident that opened hours ago.
- **Stop.** Before each step, the incident is read again. The escalation stops
  when the incident is **acknowledged**, **resolved** (or marked a false
  positive) or **muted** in the Inbox, when its signal **clears** (the scope
  stops firing), or when its policy is deleted. After the last step (and
  repeat) it ends.
- **Again.** An incident that is acknowledged, then closes and fires again,
  escalates again from its new first delivery. A re-delivery of an incident
  whose escalation already ran out does not start it over; the signal has to
  clear and fire again first.

Escalation messages are not alert deliveries: they do not appear as deliveries
in the Inbox, do not count toward an incident's deliveries, and do not reset a
rule's cooldown. **Recent escalations** on the settings page lists the latest
escalations, why each stopped, and every step with its result: sent, failed
(with the channel's error) or skipped (with the reason).

### What a step sends

The message names the project, the rule, what fired and its direction, the
step's position ("step 2 of 3"), and a link to the incident when tripl knows its
public address (`APP_BASE_URL`).

- **Webhook**: a JSON body with `"event": "tripl.escalation"`, the project,
  the incident (`correlation_group_id`, `scope_name`, `direction`,
  `started_at`, `link`), `escalation_id`, `subject` and `message`.
- **PagerDuty**: a `trigger` event with the dedup key
  `tripl-escalation-<escalation_id>`, so every step and repeat of one
  escalation folds into one PagerDuty incident, apart from the incident's own
  page. tripl does not resolve it; resolve it in PagerDuty.
- **Jira and Linear**: an issue per step, titled with the message's subject.
- **Email, Slack, Telegram, Teams**: the message as text.

### Reliability

Each step is claimed before it is sent, so two workers never send the same step
twice. A worker stopped between the claim and the send loses that step: tripl
never sends a step twice, and may, rarely, not send one at all.

## Who can see and change it

| | Owners | Admins | Members | API keys |
|---|---|---|---|---|
| Read policies, routes and recent escalations | yes | yes | no | no |
| Create, change and delete them | yes | yes | no | no |

Both need a browser session; any API key is refused with `403`. A user who is
not in the organization gets `404`.

Every change is recorded in the organization's audit log:
`org.escalation_policy.create`, `.update` and `.delete`, and
`org.alert_route.create`, `.update` and `.delete`, with the row before and
after.

## API

All under `/api/v1/orgs/{org}/escalation`, for owners and admins, from a
browser session.

| Method | Path | What |
|---|---|---|
| `GET` | `/policies` | The policies, with each step's target name |
| `POST` | `/policies` | Create a policy (`201`) |
| `GET` | `/policies/{policy_id}` | One policy |
| `PUT` | `/policies/{policy_id}` | Replace a policy |
| `DELETE` | `/policies/{policy_id}` | Delete a policy (`204`); `409` while a route uses it |
| `GET` | `/routes` | The routes, in the order they are tried |
| `POST` | `/routes` | Create a route (`201`) |
| `PUT` | `/routes/{route_id}` | Replace a route |
| `DELETE` | `/routes/{route_id}` | Delete a route (`204`) |
| `GET` | `/options` | What a step or a route may name: projects, destinations, members, groups, alert rules, kinds of signal |
| `GET` | `/escalations` | The 50 latest escalations, with their steps |

A policy body:

```json
{
  "name": "Checkout on-call",
  "description": "",
  "steps": [
    {"delay_minutes": 5, "target_type": "destination", "target_id": "…"},
    {"delay_minutes": 15, "target_type": "group", "target_id": "…"}
  ],
  "repeat_count": 1,
  "is_default": false
}
```

`target_type` is `destination`, `user` or `group`. A route body:

```json
{
  "name": "Shop drops",
  "enabled": true,
  "position": 0,
  "project_ids": ["…"],
  "rule_ids": [],
  "scope_types": ["event"],
  "direction": "drop",
  "policy_id": "…"
}
```

An id that is not the organization's (a destination of another organization's
project, a user who is not a member) is refused with `422`, as are steps out of
order, more than 10 steps or 5 repeats, and unknown fields. Two policies of one
organization cannot share a name (`409`).
