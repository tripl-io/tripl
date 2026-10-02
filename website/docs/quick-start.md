---
title: Quick Start
sidebar_label: Quick Start
sidebar_position: 2
---

# Quick Start

This guide takes you from nothing to a working tripl setup:

1. [Run tripl locally](#step-1--run-tripl) — one Docker command.
2. [Create the first account](#step-2--create-the-first-account).
3. [Explore the demo project](#step-3--explore-the-demo-project) — see the whole
   product working on realistic data, no warehouse needed (~10 minutes).
4. [Connect your own warehouse](#step-4--connect-your-warehouse) and
   [scan it into a plan](#step-5--create-your-first-scan).
5. [Watch detection come to life](#step-6--watch-the-monitors),
   [add your own metrics](#step-7--define-your-own-metrics), and
   [wire up your first alert](#step-8--set-up-your-first-alert).

Steps 1–3 need nothing but Docker and take about fifteen minutes. Steps 4–8 need
read access to a warehouse (**ClickHouse**, **BigQuery**, or **PostgreSQL**)
where analytics events already land; budget half an hour the first time.

If a term is unfamiliar along the way, [Concepts](./use/concepts.md) defines
every idea in plain language.

## Step 1 — Run tripl

You need **Docker Engine with the Compose v2 plugin** (`docker compose`, not the
legacy `docker-compose`). Clone the repository and start the dev stack — it
builds from source and needs no secrets:

```bash
git clone https://github.com/vladenisov/tripl.git
cd tripl
cp .env.example .env
docker compose -f compose.dev.yaml up --build
```

The first build takes a few minutes. When the logs settle, everything is up:

| Where | URL |
|---|---|
| App | http://localhost:5173 |
| API | http://localhost:8000 |
| API reference (interactive) | http://localhost:8000/docs |

:::note This is the trial stack, not a deployment
The dev stack runs over plain HTTP with default credentials — perfect for a
laptop, wrong for anything shared. To deploy for your team, use the hardened
production stack (`compose.yaml`, published release image, real secrets,
HTTPS in front): follow **[Self-hosting & Deployment](./run/deployment.md)**.
Steps 2 onward are identical either way.

Hacking on tripl itself? `CONTRIBUTING.md` in the repository covers hot-reload
(`--watch`), tests, and the rest of the contributor setup.
:::

## Step 2 — Create the first account

Open the app and register on the sign-in screen. **The first person to register
becomes an owner**; everyone who registers after that joins as a member.
Owner matters for later steps: only owners and admins manage data sources and
members.

After signing in you land on the workspace dashboard with two ways forward:

- **Generate demo project** — a complete synthetic project to explore. Start here.
- **New project** — an empty project for your real work.

![All projects, with Generate demo project and New project at the top right](/img/screenshots/workspace.light.webp#gh-light-mode-only)
![All projects, with Generate demo project and New project at the top right](/img/screenshots/workspace.dark.webp#gh-dark-mode-only)

## Step 3 — Explore the demo project

Click **Generate demo project**. tripl builds a realistic project — event types,
events, fields, variables, collected metrics, a few anomalies, some schema
drift — backed by a **local synthetic warehouse**. Nothing leaves the server and
no connection is made anywhere, but the product is not faked around it: real
scans, metric collection, anomaly detection, and reconciliation run over that
synthetic source, and a background clock keeps it fresh.

![The demo project on first open: the demo banner, the welcome panel and the Overview](/img/screenshots/demo-welcome.light.webp#gh-light-mode-only)
![The demo project on first open: the demo banner, the welcome panel and the Overview](/img/screenshots/demo-welcome.dark.webp#gh-dark-mode-only)

**Run the coached chapters.** The welcome panel's **Start: Run the live loop**
opens the first of the chapters — short hands-on lessons, one per product area,
each coached by a strip under the demo banner and a callout that rings the exact
button to press. **Browse chapters** lists them all. **Run the live loop** is the
product's core loop end to end:

1. **Run a scan** — press **Run now** on any scan.
2. **Watch it land** — the run completes and shows what it changed.
3. **Collect a metric** — press **Collect now** on any metric.
4. **See the chart move** — open that metric and see the series your
   collection recomputed (a daily metric gains a new point only once a day
   has closed).

That loop — *scan the warehouse, collect metrics, watch the charts* — is the
same loop your real project will run on a schedule. The other chapters walk
the rest hands-on: **Edit an event** (on `Trial Started`, replace the current
Product ID value with `prod_monthly`; the guide advances automatically, then
asks you to type `$`, select `${product_id}`, and save), **Properties & value
drift**, **Review a branch**, **Reconcile the plan**, **Route an alert**, and
**Explore the rest**.
(Prefer to read first? **Browse chapters** → **Quick overview** walks the same
surfaces without asking you to do anything.)

Then look around in roughly this order:

- **Plan → Events** — the catalog. Open an event to see its fields, values,
  tags, status, and change history.
- **Observe → Overview** — the health of the whole project at a glance.
- **Observe → Alerting → Rules** — the demo's alert rules, each with its
  current state and the condition it watches for. To study a signal's volume
  chart, forecast, heatmap, and the breakdown of what moved, open the event (or
  click its signal) — that opens the monitoring detail.
- **Govern → Reconciliation** — what is documented-but-dead and
  live-but-undocumented.

Reset or delete the demo any time — it never touches real projects. The full
list of what is synthetic, what really executes, and what is intentionally
unavailable is in **[The demo workspace](./use/demo-workspace.md)**.

:::tip Done exploring?
If the demo answered your questions, you already know the product. The rest of
this guide repeats the same loop against **your** data. The demo says the same
thing where you finish: completing the last coached chapter offers **Create a
real project**, and the same link sits in the expanded welcome panel. Both go to
the dashboard, where **New project** starts an empty one to connect your own
warehouse.
:::

## Step 4 — Connect your warehouse

Create a project for your real work (**New project** on the dashboard), then
point tripl at your warehouse. tripl only ever **reads** from it — it never
writes, and it stores only the aggregated counts it needs, never your raw
events.

**New project** can also start from an industry template. Its starter plan
opens as a draft branch to review and merge, and main stays empty until you do.
See **[Project templates](./use/project-templates.md)**.

1. Open **Settings → Data sources** (in the **Organization** group). Data
   sources are shared by every project in the organization, and only **owners**
   and **admins** manage them.
2. Add a connection and fill in the details for your warehouse:
   - **ClickHouse** — host, port (8123), database, username, password.
   - **PostgreSQL** — host, port (5432), database, username, password.
     **Version 14 or newer is required.** SSL mode left unset resolves to
     `require` for remote hosts (`prefer` for localhost); choose `verify-full`
     with a CA certificate to also authenticate the server.
   - **BigQuery** — GCP project ID, a default dataset, and a service-account
     JSON key pasted into the form. A **max billed bytes** guard (100 GiB by
     default) caps query cost.
3. Save, then click **Test** on the connection card and wait for it to go
   green.

   ![The New data source dialog, with the fields for ClickHouse](/img/screenshots/data-source-add.light.webp#gh-light-mode-only)
   ![The New data source dialog, with the fields for ClickHouse](/img/screenshots/data-source-add.dark.webp#gh-dark-mode-only)

:::info The three warehouses are not interchangeable
They support the same features with different guarantees and dialect details.
Before committing to one, skim the
**[warehouse capability matrix](./develop/warehouse-parity.md)** — it states
per capability what is proven, what is believed, and what is bounded, plus
per-warehouse permissions and setup requirements.
:::

## Step 5 — Create your first scan

A **scan** reads a warehouse table. Every run **drafts your plan** — proposing
events, fields, and value lists from what it actually finds. A *monitoring* scan
also runs on a schedule and **collects the volume counts** that power monitoring;
a *Catalog only* scan has no schedule and collects none.
It is the fastest way to turn an existing events table into a written,
monitored tracking plan.

1. Open **Govern → Scans** in your project (route: `/p/<slug>/scans`) and create
   a scan.

   ![The New scan form: what the scan does, its data source, the base query and the preview](/img/screenshots/scan-new.light.webp#gh-light-mode-only)
   ![The New scan form: what the scan does, its data source, the base query and the preview](/img/screenshots/scan-new.dark.webp#gh-dark-mode-only)
2. Answer **What this scan does** — it is the first question on the form, and it
   decides everything else:
   - **Catalog + monitoring** — adds events and fields to your tracking plan
     *and* records metric points, so anomalies and alerts can fire. This is the
     default, and it needs a **time column** and a **schedule**. Pick this one if
     you are following this guide; Step 6 depends on it.
   - **Catalog only** — adds events and fields to your tracking plan when you
     run it, nothing more. No schedule, so no metric points, no anomalies and no
     alerts. It can still take a **time column**, which bounds each run to the
     lookback window rather than reading the whole query.
3. Point it at your data: pick the **data source** and give the **base query** —
   typically just selecting from the table where your events land.
4. **Load the preview.** It reads a few rows from your query so the pickers
   below it can offer your real columns instead of a blank box. Nothing is
   written.
5. Answer the questions those columns unlock, top to bottom:
   - **How events are stored** — if your table has one row per event, with a
     column naming the event and a JSON column holding its properties, choose
     **Event + properties** and pick those two columns (**Event column** and
     **Properties column**). Each event name becomes an event, and every key of
     the JSON becomes one of its properties; nothing else about naming needs
     setting. See
     [The Event + properties setup](./use/feature-reference.md#event-properties-setup).
     Otherwise keep **Custom** and answer **Event type** below.
   - **Event type** (Custom) — give every row the same event type, or leave it on *Name
     events from a column* and pick the **Event type column** whose values are
     the event names. One of the two is required: a scan with neither cannot name
     a single event, and **Create scan** stays disabled until you answer.
   - **Time column** — the timestamp tripl buckets counts by, and what bounds
     each run to the lookback window.
   - **Schedule** (Catalog + monitoring only) — every 15 minutes, hourly, every
     6 hours, daily, or weekly.

   In Catalog + monitoring the form will not let you create the scan until the
   time column and the schedule are both set, because a monitoring scan missing
   either one is never scheduled and collects nothing. In Catalog only the time
   column is optional; leaving it empty means every run reads everything the base
   query returns.
6. **Read what this scan would create.** At the foot of that same block —
   *after* the fields it is computed from, so answering them cannot invalidate
   it — the preview panel leads with **What this scan would create**: the event
   names tripl would add to your plan and the fields it would add with them,
   worked out by pushing the sampled rows through the *same* planner a real run
   uses. The sample warehouse rows are kept underneath, under **Show sample
   rows** — they are the evidence, not the answer.

   The answer is bounded, and says which bound it is under. It reads the scan's
   lookback window — or, if the scan has no time column to window on, everything
   the base query returns, which the panel states — and at most the 5,000 most
   common column combinations, so when it hits that cap it reads *Would create
   **at least** N events* rather than a flat count. It never projects a
   table-wide total.

   Change the form afterwards — a different event name format, a different
   cardinality threshold — and the panel says the answer no longer describes this
   scan; **Recalculate** re-runs it. If your **Event name format** references a
   key the rows cannot supply, the preview says so here: that format would fail
   *every* run of the config, so this is the cheapest place to find out.
7. Open **Event names and grouping** and **App version** if you need them. These
   sections start collapsed on purpose: leaving them alone gives sensible
   behaviour, and each says what that behaviour is. Version and platform columns
   unlock release-regression tracking and per-platform breakdowns later, so set
   them if you have them.
8. Run it, then open **Review events** and triage the draft: keep what makes
   sense, fix descriptions and types, flag fields that carry personal data,
   and delete the noise.

Starting a scan creates a **run** — watch its status and progress under the scan,
and use **Run again** if one fails.

### What happens after a scan runs

Every run adds events and fields to your tracking plan. A **Catalog +
monitoring** scan also records **metric points** on its schedule, and those
points are what anomaly detection and alerts are built on:

**events → metric points → signals → alerts**

- **Catalog + monitoring** — the scan adds events to your tracking plan on every
  run, and records metric points on its schedule. Anomaly detection reads those
  points and raises signals; alerts are sent from signals. **Run now** fills the
  plan and writes no metric point; the one manual metrics path is **Replay a
  period…** in the scan page header, which opens a dialog that fills a past
  window and unlocks with the same time column and schedule.
- **Catalog only** — the scan adds events and fields to your tracking plan. It
  records no metric points, so it raises no anomalies and sends no alerts.

Each scan restates its own half of that chain: the scans list says it once, the
scan form says it under the mode you have selected, and a scan's own page says
what *that* scan does today. A scan with a schedule but no time column is never
picked up by the scheduler, and says so — its badge reads **Needs a time
column**, and its page adds that runs you start by hand still add events to your
plan.

A monitoring scan that falls behind shows a **freshness** chip: **Data late ·
&lt;lag&gt;** (the newest data is older than the scan's interval allows) or
**Scan overdue** (the scan has not completed a collection on schedule). A fresh
scan, a manual scan, or one with nothing collected yet shows no chip. While a
scan is late or overdue, its volume drops are
held instead of raised, so a delayed warehouse load does not show up as a drop
on every event. See
[Source freshness](./use/feature-reference.md#source-freshness).

The chain also runs backwards, which is how you get from an alert to its cause.
An alert names the scan it came from, so **Govern → Scans** is where you start:
open that scan, expand the run in **Recent runs**, and its **Run details** puts
you one click from the rest. **Signals added** opens
[Anomalies](./use/feature-reference.md) filtered to that scan, and **Alerts
queued** opens the alerting audit log filtered the same way. A counter of `0` is
plain text, not a link, because there would be nothing on the other side.

Those two pages answer *what is open now*, while the run counts *what that run
raised* — so an older run can link to a page with nothing on it. When that
happens the page says so, and keeps the scan selected rather than quietly
showing you a different scan's anomalies.

:::note Scanning is optional — you can also write the plan by hand
Events, event types, fields, and variables can all be created manually under
**Plan**, and most teams do a bit of both: scan to bootstrap, edit by hand to
polish. See [the user guide](./use/user-guide.md#plan-write-down-what-should-be-tracked)
for the manual route, and note that once the plan is live you should make
changes on **plan branches** with review, like pull requests.
:::

## Step 6 — Watch the monitors

With a scan collecting on a schedule, detection comes to life on its own — tripl
learns every event's normal rhythm (including time-of-day and day-of-week
patterns) and raises a **signal** on an unexpected spike, drop, or change of
shape. There is nothing to set up.

- **Observe → Overview** — the whole project at a glance.
- **Observe → Alerting → Rules** — each **alert rule** here is attached to a
  scope, listed with the condition it watches for, where it
  routes, and its current state. Open an event (or one of its signals) for the
  full drilldown — the **monitoring detail**: volume chart with a short
  forecast, heatmap by hour and weekday, value-distribution drift, and
  breakdowns of which slice moved. (The rest of what happens to a firing signal
  is on the other Alerting tabs — see Step 8.)

:::note "No data yet" is normal at first
These views read from collected metrics, so they stay empty until scans have
actually gathered counts — hours to days depending on your scan schedule. A
message like *"run a scan to start collecting volume metrics"* means exactly
that, not that something is broken.
:::

![An event with a spike: the chart, the signal and which platform the change came from](/img/screenshots/event-monitoring.light.webp#gh-light-mode-only)
![An event with a spike: the chart, the signal and which platform the change came from](/img/screenshots/event-monitoring.dark.webp#gh-dark-mode-only)

If detection is too twitchy or too quiet, tune the thresholds in the project's
**Detection settings** — [How anomaly detection works](./use/anomaly-detection.md)
explains what each knob does.

## Step 7 — Define your own metrics

Event volume is monitored automatically. For the numbers that aren't an event
count — revenue per hour, checkout success rate, sign-ups per active user —
define **metrics** under **Observe → Metrics**. A metric produces one number per
time bucket and is monitored exactly like an event. Pick a kind:

- **From tracked events** — built from events you already collect, with no
  warehouse query of its own: a single event's count, a ratio of one event to
  another (A / B), or an event per distinct user. **Start here** — if your scan
  is collecting, an event-composition metric produces values immediately.
- **Custom SQL** — a read-only `SELECT` you write, run against your warehouse on its
  own interval; you pick the time column and tripl buckets the results.
- **From a fact table** — an aggregation (`count`, `sum`, `avg`, `min`, `max`,
  `count_distinct`) or a ratio of two aggregations over a **fact table** — a
  reusable read-only query you define once under **Observe → Metrics → Fact
  tables** and slice with named filters and breakdowns across many metrics.

![Observe → Metrics: each metric with its latest value, trend and status](/img/screenshots/metrics.light.webp#gh-light-mode-only)
![Observe → Metrics: each metric with its latest value, trend and status](/img/screenshots/metrics.dark.webp#gh-dark-mode-only)

The create form ends in **Create and start collecting**, or **Save as draft**
when the definition is not ready: a draft is not collected until you press
**Activate** on its page. On an active metric, press **Collect now** (or
**Recompute**, as it reads on a fact-table metric) to get a first
data point without waiting for the schedule, then open the metric — its
drilldown has the same tabs (volume, heatmap, distribution, breakdowns) as any
event's monitoring detail.

## Step 8 — Set up your first alert

A signal only helps if someone hears about it. Open **Observe → Alerting**:

1. **Add a destination** — where alerts go: Slack, Telegram, email, a generic
   webhook, Jira, or Linear. Mark it **enabled**.
2. **Create a rule** — which signals are worth interrupting someone for: the
   scope, the direction (spikes, drops, or both), how big a change must be, and
   a **cooldown** so one problem doesn't page you repeatedly.
3. **Replay it before enabling.** The rule's **Replay** runs recent days of real
   data against it and shows exactly what it *would* have sent. Tune until it's
   signal rather than noise, then switch it on.

![The New alert rule dialog: what to watch, when, and the cooldown](/img/screenshots/alert-rule-new.light.webp#gh-light-mode-only)
![The New alert rule dialog: what to watch, when, and the cooldown](/img/screenshots/alert-rule-new.dark.webp#gh-dark-mode-only)

Every alert that goes out is recorded under **Deliveries**, and the **Inbox**
groups correlated alerts so you can acknowledge, resolve, or mute a whole
incident at once. The full rule syntax and routing options are in
**[Alerting rules](./use/alerting.md)**.

## You're set up

You now have the full loop running: a plan drafted from real data, scans
keeping it honest on a schedule, detection learning what normal looks like, your
own metrics collecting, and an alert rule that tells the right person when the
numbers move. From here:

- **[How-to guides](./how-to/index.md)** — the everyday jobs, one page each,
  with screenshots.
- **[User guide](./use/user-guide.md)** — the full task-oriented walkthrough,
  including plan branches with review, variables, event lifecycle, and a
  realistic first-week rollout plan.
- **[Concepts](./use/concepts.md)** — every idea defined in plain language.
- **[Variables & templates](./use/variables-and-templates.md)** — reusable
  documented values and drift review.
- **[Agent & API guide](./integrate/agent-api-guide.md)** — drive tripl from
  scripts and LLM agents with scoped API keys.
- **[Self-hosting & Deployment](./run/deployment.md)** — move from the trial
  stack to a real deployment.
- **[Troubleshooting & FAQ](./use/troubleshooting.md)** — when something looks
  wrong.
