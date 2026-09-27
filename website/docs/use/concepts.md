---
title: Concepts
sidebar_position: 1
---

# Concepts

This page explains the ideas behind tripl in plain language. Read it once and
the rest of the product — and the rest of the docs — will make sense. No setup
or code required.

If you're impatient: tripl helps you **write down what your product should
track**, **check it against what's really happening**, and **get told when the
two stop matching**.

Inside a project the same glossary is one click away: a small info icon
beside the Scans, Coverage, Reconciliation and Anomalies headings (and beside
Metrics while the Fact tables tab is open) shows the term's one-line
definition on hover and opens its entry on the in-app Concepts page.

---

## The big picture

There are three things tripl helps you do, and they build on each other:

1. **Plan** — describe the events and data your product is supposed to send.
2. **Observe** — pull the real numbers from your warehouse and watch them.
3. **React** — get alerted when reality drifts from the plan.

Everything below is a piece of one of those three jobs.

---

## A project

A **project** is one tracking plan and everything around it. If your company has
an iOS app, an Android app, and a website that share the same analytics, that's
usually one project. If you run two products that have nothing to do with each
other, that's two projects. Each project has its own plan, scans, metrics, and
alerts. Users and data-source connections belong to the workspace; an API key
can still be restricted to one project slug.

---

## The building blocks of a plan

These are the pieces you arrange to describe what should be tracked.

### Event

An **event** is one thing that happens in your product that you care about:
`checkout_completed`, `video_played`, `signup_started`. It's the central object
in tripl. An event has a name, a description, the fields it carries, and a
lifecycle status: `draft`, `in_review`, `ready_for_dev`, `implemented`, `live`,
`deprecated`, or `archived`. Where a scan names events, the name is also the
event's *scan identity* — the key collection matches on; an optional **title**
is a free-text label shown beside it and never part of the identity. Review
state, owner, and an optional deprecation sunset date add workflow context
without changing the event's identity. The step to `live` is taken by the data:
the first scan that sees an approved event with volume promotes it and records
when it was first seen, and a daily check flags retired events that keep firing
past their sunset date or whose replacement stays silent.

### Event type

An **event type** is a folder for related events — a way to keep a catalog of
hundreds of events organised. For example, a `Commerce` event type might contain
`cart_opened`, `checkout_started`, and `checkout_completed`.

### Field

A **field** is a piece of data attached to an event — like `price`, `currency`,
or `screen_name`. Each field has a type and a description, and can be marked as
carrying **personal or sensitive data** so everyone knows to handle it carefully.

### Meta field

A **meta field** is data that rides along with *every* event, not just one — the
app version, the platform, the user's country. You define it once at the project
level instead of repeating it on every event.

### Variable

A **variable** is a typed, reusable `${placeholder}`. It separates the contract
your team documents from the values a scan observes. A variable can hold a
global documented-value list, warehouse column or dotted JSON-path bindings,
and a complete per-event override when one event is the exception. Scans use the
bindings to adopt the same variable, report novel values as drift, and preserve
hand-authored event field values instead of overwriting them. A scan-created
variable that nothing in the plan refers to any more is retired at the end of a
later catalog run — always by a manual scan, and by a scheduled collection too,
which judges a variable minted from a JSON path on every run but one minted from
a scalar column only when the config declares a lookback window, since a run
reading one interval cannot tell a quiet scalar column from an enumerable one;
one a person renamed, edited, documented, or excluded from scans is kept. See
[Variables & templates](./variables-and-templates.md) for the full workflow.

### Relation

A **relation** records how events connect to each other — for example, that
`checkout_started` is expected to be followed by `checkout_completed`. It
captures the structure of a flow, not just the individual events. Relations
are created, edited in place (**Edit** on a relation's row) and deleted from
**Plan › Relations**.

### Metric

A **metric** is the number-shaped counterpart to an event: a value you watch over
time rather than a thing that happens. "Revenue per hour", "the share of checkouts
that succeed", or "sign-ups per active user" are metrics. Like an event, you define
it yourself — but instead of a stream of occurrences it produces **one number per
time bucket**. A metric is one of three kinds:

- **SQL** — a read-only `SELECT` or top-level `WITH ... SELECT` you write that
  returns one value per bucket, run against a data source on its own interval.
- **Fact** — a count, sum, average, min, max, or distinct count over a reusable
  fact table. A fact metric can be one aggregate or a ratio of two operands;
  named filters, structured conditions, raw filter fragments, and breakdowns
  are optional.
- **Event composition** — built from events you already collect: a single event's
  count, a ratio of one event to another, or an event per distinct user.

Each metric has a lifecycle — **draft**, **active**, **archived** — and once
active it is monitored exactly like an event. Only an active metric is monitored:
archiving one (or putting it back in draft) stops its collection, closes any open
signal on every surface and withdraws it from alerting, while the anomalies it
already recorded stay on its chart as history. Unlike the rest of the plan,
metrics are **project-wide and aren't branched**.

---

## Branches: changing the plan safely

Here's the idea that makes tripl different from a spreadsheet.

The plan everyone trusts is the **main** plan. You never edit it directly.
Instead, you create a **branch** — a private copy of the whole plan. You make
your changes there, share the branch for **review**, and only when it's approved
do you **merge** it back into main.

If that sounds like how teams change code with pull requests, that's exactly the
point. It means:

- The live plan is never half-finished or broken while you work.
- Every change gets a second pair of eyes before it lands.
- You can see precisely what changed on a branch since it was created (the
  **diff**). Changes that landed only on main are shown separately as the branch
  being behind; they are not misreported as branch deletions.

A couple of things tripl handles for you so merging is safe:

- **Identities survive a merge.** When your branch and main both touched the
  same event, tripl matches them up by name rather than creating a duplicate, so
  the metrics, history, and alerts already attached to that event stay attached.
- **Non-conflicting edits merge automatically.** If you changed an event
  description while a teammate added a tag, field value, photo comment, or
  another child on main, both land. If both sides changed the same state to
  different values, tripl reports a conflict rather than overwriting either
  side. **Update from main** brings main's newer changes into the branch and
  asks, for each overlap, whether to **keep this branch**'s version or **take
  main**'s. Branches created before complete merge baselines were introduced
  cannot be updated; copy their changes to a new branch.
- **Owners can gate their event types.** An event type can have **owners**;
  merging a branch that adds, removes, or edits the type itself (its display
  name, description, color, or order) requires a sign-off from one of them.
  Changes to only the type's fields or events do not ask for one.

---

## Connecting real data

A plan is only half the story. The other half is what your apps actually send.

### Data source

A **data source** is a connection to a data warehouse where your real analytics
events land — **ClickHouse**, **BigQuery**, or **PostgreSQL**. tripl reads from
it; it never writes to it. Your warehouse stays entirely under your control.

### Scan

A **scan** points at a table in your warehouse and reads what's really there.
From the real columns and values it sees, it can **propose events, fields, and
value lists automatically**. Two ways to use it:

- **Bootstrapping** — you have data but no written plan yet; a scan gives you a
  first draft in minutes.
- **Keeping in step** — you have a plan; a scan tells you where reality has
  moved on without it.

### Monitoring scan vs catalog-only scan

Every scan is one of two things, and you choose which when you create it:

- A **monitoring scan** ingests events into your plan **and** records metric
  points on a schedule. It needs a **time column** (what tripl buckets the
  counts by) and a **schedule** (how often it runs). Both are required, because
  without either one the scheduler never picks the scan up.
- A **catalog-only scan** discovers events and fields and stops there. It has no
  schedule — that absence is what makes it catalog-only — so it runs when you
  start it and records no metric points. It may still name a **time column**,
  which does not turn it into a monitoring scan: there it only bounds each run to
  the lookback window instead of reading everything the base query returns.

**Only a monitoring scan produces metric points, and metric points are what
everything downstream is built on** — no points means no [signals](#monitor--signal),
and no signals means no [alerts](#alert-rule). A catalog-only scan is a perfectly
good choice when you only want to keep the plan honest; it is a bad surprise when
you expected to be watched. The badge on each scan's row tells you which one you
have.

### Event counts

Behind monitoring, a **monitoring** scan rolls your raw events up into **counts
over slices of time** — "how many `checkout_completed` events happened each
hour". tripl collects these on that scan's schedule and stores them, building up
the history that monitoring — and event-composition [metrics](#metric) — needs.
A catalog-only scan has no schedule, so it contributes none of this history.

---

## Watching the data

### Monitor & signal

tripl itself learns what "normal" looks like for every event — including the
fact that traffic is higher at lunchtime than at 3am, and higher on weekdays
than weekends — and raises a **signal** when the real numbers stray too far
from that expectation. Signals come in flavours: a sudden **spike**, an
unexpected **drop**, or a change in **shape**. This detection is automatic;
there is nothing to set up.

A **monitor** is an [alert rule](#alert-rule) attached to a scope, together
with its live state — firing, warning, or healthy (a monitor can also be
muted, which is an independent flag). The **Observe → Alerting → Monitors**
tab lists them. A monitor doesn't detect anything itself: it decides which
signals matter and where they go. "Monitor" and "rule" name the same object,
and they share one screen.

Detection watches three levels: the **whole project**, an **event type**, or a
single **event** — and each active **metric** is watched the same way, as its
own scope. The per-event volume drilldown — chart, forecast, heatmap — is the
**monitoring detail**, reached from an event or one of its signals.

### Schema drift

**Schema drift** is when the *structure* of your data changes, not just the
volume: a field shows up that you never documented, one you relied on stops
appearing, or a field starts carrying values it never used to. tripl watches for
this and surfaces it alongside the catalog.

### Variable value drift

**Variable value drift** is when a bound variable starts carrying values outside
its effective documented list. It is reviewed per event: accept the new values
globally or only for that event, snooze the evidence, mark it false-positive, or
reopen it. Unlike observed samples, accepting a drift changes the plan contract.
An acceptance covers exactly the values you accepted — a later scan reopens the
drift when it sees a value outside that set.

### Reconciliation

**Reconciliation** is the plan-versus-reality report. It answers two questions
at once:

- **Dead events** — documented in the plan, but no longer arriving in the data.
- **Undocumented events** — arriving in the data, but missing from the plan.

It's the fastest way to find the gaps between what you think you track and what
you actually track.

### App version & release regression

A scan or catalog metric can name an optional **app version column**. When it
does, tripl keeps a metric series per release. One project-level **Releases to
keep** setting under **Settings → Project → General** controls how many latest
releases stay explicit across event monitoring, adoption, project totals, and
SQL/fact/event-composition metrics; older releases fold into a single
**"Other"** bucket. "Latest" follows version order: SemVer precedence, and
plain dotted numbers that are not strict SemVer (`15.8`, `15.10`, `1.2.3.4`)
compared numerically, so `15.10` is newer than `15.9`. Event scans also watch for a **release regression**: an
event that disappeared or fired far less in the newest release than in the one
before it.

New projects retain five releases by default; you can raise or lower that value
for the product's release cadence.

Releases roll out gradually, and a build spends its first stretch seen only by
developers and testers, so tripl judges a release only once it takes a real
share of the project's version traffic. The same project-total maturity status
is used by every event and catalog metric on that source, so a rare event or a
ratio cannot make an already rolled-out release look like a pre-release. tripl
then compares the new release against the previous one over the window where
both are live. That catches "we shipped 2.4 and
`checkout_completed` stopped firing" without crying wolf during the rollout.

When a standalone SQL or fact metric is not already tied to a scan, tripl uses
the version-enabled scan on that metric's data source with the largest
project-total volume in the selected window. It deliberately does not merge
overlapping scans, which could double-count traffic or mix bucket grids.

Per-version lines are observational rollout views, not stable cohorts for the
generic breakdown detector. Their expected ramp-up and decline do not produce
regular anomaly markers or appear as movers in unrelated volume alerts. Other
breakdowns — such as country, platform, or plan — keep their normal anomaly
monitoring; release-specific problems use the dedicated regression detector
above.

The column is **optional**: scans without one (a web stream, say) behave exactly
as before — no per-version series, no regressions, nothing extra to see.

---

## Reacting

### Alert destination

An **alert destination** is somewhere a notification can be sent: **Slack**,
**Telegram**, **email**, a generic **webhook**, **Jira**, or **Linear**. You set
these up once per project.

### Alert rule

An **alert rule** decides *which* signals are worth interrupting someone for, and
*where* to send them. A rule can filter by scope, by direction (only spikes,
only drops, or both), by how big the change is, and it can stay quiet for a
**cooldown** period so the same underlying problem doesn't page you over and
over. It also defines the **message template** — what the notification actually
says.

An alert rule and a [monitor](#monitor--signal) are two views of the same
thing: the rule is the configuration, and the monitor is that rule as you see
it live on the Monitors page, together with its current state. Detection and
rules work in sequence: tripl's automatic detection decides *whether* something
looks wrong (it raises signals), while an **alert rule** decides *whether a
signal is worth telling you about* and *where to send it* — it routes signals
to a [destination](#alert-destination).

### Simulator

Before you switch a rule on, the **simulator** replays the last several days of
real data against it and shows you what it *would* have sent. It's how you tune a
rule to be useful without first living through a week of noisy or missed alerts.

### Delivery

A **delivery** is the record of one alert that was actually sent — what it said,
where it went, and whether it succeeded. The delivery history is your paper trail
for everything tripl has told the team.

---

## Staying in control

### Roles

Every workspace member has a **role** that applies across the instance:

- **Viewer** — can look, can't change anything. Pages show a viewer what there
  is to read and leave out the buttons that change it, with one line saying so.
- **Editor** — can change the plan and alerts, and run scans, in the projects
  an owner created and in their own. A demo, or a project another editor
  created, is that person's to change (and an owner's). A project's own name,
  slug and retention can be changed only by the person who created it or an
  owner.
- **Owner** — full control, including data sources, writing and changing scans,
  managing people, and deleting the project.

### API key

An **API key** lets a script or an AI agent talk to tripl without a human logged
in. Keys are scoped to be safe: **read** keys can only look, **write** keys can
edit, and a key can be locked to a single project and given an expiry date. They
can be revoked at any time. Creating and revoking keys requires an interactive
session; a Bearer key cannot manage keys. See **[agent-api-guide.md](../integrate/agent-api-guide.md)** for
the details.

### Audit log

The **audit log** records every meaningful change in a project — who did it, what
they changed, and when — so you can always answer "how did this get like this?".

---

## Where to go next

- Want to actually do something? → **[user-guide.md](user-guide.md)**
- Connecting an agent or a script? → **[agent-api-guide.md](../integrate/agent-api-guide.md)**
- Curious how it's built? → **[architecture.md](../develop/architecture.md)**
