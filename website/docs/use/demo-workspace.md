---
title: The demo project
sidebar_position: 3
---

# The demo project

The **Generate demo project** button creates a complete project so you can explore
every part of tripl in about a minute, without connecting a warehouse of your own
or sending anything anywhere. It is built the way a real project starts, and one
rule governs it: **the plan is authored; observations are scanned.** A versioned
recipe writes the tracking plan a team would have written by hand. Everything
observed — volumes, anomalies, drift, release regressions, shadow events, metric
values, alert deliveries — comes from tripl's own pipeline reading the **demo
warehouse**, a database of generated traffic that every demo on the instance
shares.

This page is the source of truth for what a demo contains, where its data comes
from, and what it deliberately cannot do.

## What the recipe authors

Everything in this section is written by the recipe (demo data version 5), the
same for every demo, and is what a team writes rather than what a warehouse
reports.

- **The tracking plan.** 11 event types (`screen_view`, `click`, `purchase`,
  `search`, `content`, `meal_plan`, `notification`, `account`, `app_lifecycle`,
  `error`, `experiment`) and **157 events** in every status a living plan holds:
  live and implemented, in review, ready for development, drafts, one deprecated
  with a successor and two archived. **67** of them fire in the warehouse; the
  rest are planned or retired work, so their coverage reads honestly empty.
  Every event carries its fields, meta fields, properties and tags, and the
  event types have an owner.
- **Variables.** `platform`, `product_id`, `user_id` and `session_id`, with their
  documented values. **Trial Started** is documented to sell `prod_monthly` and
  `prod_annual` only, and **Home Screen View** requires `${platform}`.
- **The connection.** One data source of the *demo* kind and one scan, *Demo
  scan*, reading the warehouse's `events` table in hourly buckets, with platform
  and app-version columns, three properties whose values it records
  (`properties.source`, `properties.payment_method`, `properties.error_code`),
  and distribution drift watched on the user's country.
- **Detection settings.** Anomaly detection on with the demo thresholds and no
  ingestion-settling delay (a demo warehouse delivers every hour complete),
  platform breakdowns off, one per-scope override that quietens the noisy
  **App Crash** series, and a field contract on **Purchase Completed**: an
  `amount` below 0.01 is a schema finding. No holiday calendar: a holiday would
  mark that day's incident as planned, and a demo created on it would open with
  an empty alert inbox.
- **The metrics catalog.** The **Orders** fact table and four metrics: *Active
  Sessions* (SQL), *Purchase conversion* (Purchase Completed over Paywall View), *Revenue
  (completed)* split by the buyer's country, and *Average order value*.
- **Alerting.** A local sink, a **flaky** twin of it, a visibly disabled Slack
  example carrying no credentials, and three rules: *Spike & drift watch (demo)*,
  *Volume watch (flaky sink)* and the usually silent *Weekly release health
  (quiet)*. What they can and cannot send is described below.
- **What the team decided.** One pair of events that only look alike
  (**Push Permission Granted** and **Push Permission Denied**) dismissed as not
  a duplicate, beside a real near-duplicate left open for the duplicates view
  (**Recipe Rated** and `recipe_rated`); watches on **Paywall
  View**, **Purchase Completed**, the `purchase` event type and *Revenue
  (completed)*; and two docs pages, *Tracking plan conventions* and the *Weekly
  promo runbook*, linking the plan entities they talk about.
- **Plan history.** An edit history on the events, a Figma spec embed with a
  pinned comment, a few comment threads, one branch already merged
  (`feature/meal-planner-tracking`) and one open and under review, with exactly
  one pending change.
- **The audit trail.** The actions *you* take in the demo go through the same
  audited service paths as a real project and show up in **Govern → Audit log**.
  The recipe writes the plan directly rather than through those paths, so it
  also backfills a matching trail: one entry per authored event, event type,
  field, meta field, property, scan, alert destination and rule, attributed to
  whoever generated the demo and back‑dated so the log reads as a build‑up.
  Those entries are marked `demo_seed` in their payload. The event entries are
  dated from the events themselves: a creation carries the event's own
  first‑seen date, and the edits behind the demo's shipped, archived and
  deprecated events carry the instant their **event history** records. The one
  entry not marked `demo_seed`, *Created project*, is dated when you generated
  the demo. Connecting the warehouse is recorded too, but a data source belongs
  to the organization rather than to one project, so that entry carries no
  project and this tab does not list it.

## What the pipeline observes

A new demo then runs, in the background and on the worker, what a team's first
weeks with tripl would. The creation dialog names each stage as the server
reaches it:

1. **Scan** — the real catalog scan over the warehouse. It matches the authored
   events by name, records the values each field and property was seen with,
   and finds the open value drift on **Trial Started**: the warehouse also sells
   `prod_weekly`, which the plan does not document. The demo's scan is set to
   [send new events to the reconciliation inbox](./feature-reference.md#new-events-catalog-or-inbox),
   so the two names no plan documents stay out of the catalog.
2. **History** — a real metrics replay over the last two days, in one-day
   chunks; the dialog shows how far through it is. One job scores them once
   and dispatches what it finds. The last window is the scheduler's first live
   collection: it runs the field-contract check (the GBP purchases and the
   zero amounts) and starts the source's freshness watermarks.
3. **Signals** — the catalog metrics through the worker's own collectors, the
   event lifecycle pass, and the search index.

The demo opens there, about half a minute after you asked for it. The rest of
the history, back to
[`DEMO_HISTORY_DAYS`](../run/configuration.md#demo-workspace) days (23 by
default: three weekly cycles for the hour-of-week baseline, plus two days to
score), is **filled in afterwards** by a second task on the worker, while you
look around; the demo's banner says *Filling in history* with how far it is,
and **Run now** and **Replay** on the demo's scan wait until it is done
(a few minutes). The fill replays the older weeks, then scores the last three
weeks once on the full baseline and dispatches what it finds, so the newest
incident files the alert deliveries a live collection would have; then it
marks the weekly promo sends and tags the planned window, the steps that need
the whole history.

What you land on is therefore the pipeline's: anomalies on the warehouse's
incidents and their attributions, distribution drift on the country mix,
release markers and regressions on the **By version** tab, the GBP currency
and the zero amounts as schema drift (field-contract violations), the
`results_count` string on iOS as property drift, the two unplanned names as
shadow-event candidates, coverage, and the deliveries those signals sent.

A few things are human reactions to what was observed rather than
observations, and the recipe writes them after the pipeline, stated here so
nothing on screen pretends otherwise:

- **The dead event.** **Subscription Cancelled** was last seen 45 days ago,
  earlier than any warehouse keeps, so that date is authored. The warehouse
  never sends it, so no scan brings it back to life.
- **The promo verdicts.** Every *Weekly promo email* send in the history is
  marked **Expected → campaign** with a note, so the Annotations page suggests
  planning the next ones; and one [expected
  window](./feature-reference.md#planned-events), *Spring promo (planned)*, is
  drawn around the best recent day of *Revenue (completed)*: that chart shades
  the window and draws the lift muted, and it raises no alert.
- **A note on the newest incident**, left in the inbox by someone looking into it.

## The demo warehouse

The warehouse is a PostgreSQL database, `<your database>_demo` (`tripl_demo` in
the bundled compose stack), on the server tripl already uses. It holds two
tables in its `warehouse` schema, `events` and `orders`, partitioned by day, and
generated from one deterministic model of a recipe app: the same rows every time
for the same hour, whoever reads them.

- **Who writes it.** The worker's `write-demo-warehouse` beat task, every five
  minutes, writes the rows of the slice it runs in, so an hour is complete before
  it ends. Its first run backfills the whole retention window (about 800,000 rows,
  under a minute); after downtime it catches up from where it stopped; when the
  model changes in a new release it rewrites everything. `python -m tripl.migrate`
  creates the database, the reader role, the schema and the grants, and repairs
  them on every run.
- **Who reads it.** Demos read it as `tripl_demo_reader`, a login role that owns
  nothing and holds at most 20 connections. Its sessions are read-only, with a
  15‑second statement timeout, a 30‑second idle-in-transaction timeout, 16 MB of
  work memory and 256 MB of temporary files, and tripl re-applies those settings
  before every statement it sends, so a query cannot switch them off for the next
  one. It can read the two warehouse tables and nothing else: nothing is granted
  to it in tripl's own database, and PUBLIC loses its privileges in the demo one.
  Its password is derived from `SECRET_KEY` unless you provision the role
  yourself. Every connection parameter comes from the server's configuration,
  never from the data source: a demo source's host and credentials are ignored,
  so nothing a user can edit reaches that connection.
- **How big it gets.** Day partitions older than
  [`DEMO_WAREHOUSE_RETENTION_DAYS`](../run/configuration.md#demo-workspace) (30)
  are dropped. Over `DEMO_WAREHOUSE_MAX_MB` (1024), the oldest days beyond the 23
  a demo replays are dropped too and an error is logged: the cap is a backstop,
  not the plan.
- **Your own database stays bounded too.** A demo project would otherwise keep
  every collected hour forever, and a public demo creates demos by the hundred.
  The daily `prune-demo-history` task deletes a demo's *observed* rows that fall
  behind the warehouse's retention; the authored plan is never touched. A
  collection that resumes after a long pause starts no further back than the
  warehouse still holds, so it never scores a gap as zeros.
- **SQL against it.** On PostgreSQL, the SQL editor and SQL metrics run real SQL
  against the two tables, read-only and time-limited.

### Two engines

`DEMO_WAREHOUSE_MODE` picks what a demo reads:

- **`auto`** (the default) — the PostgreSQL warehouse when it is ready, and the
  **in-memory engine** otherwise. A reader connection that fails falls back to
  memory and is remembered for a minute, so a down warehouse costs one connection
  attempt a minute, not one per scan.
- **`postgres`** — always the warehouse; a failure is the scan's error.
- **`memory`** — always the in-memory engine.

The in-memory engine generates the same rows from the same model on demand, an
hour at a time, so a demo that switches engines between two scans reads the same
history either way. It answers the queries the demo itself runs and returns a
clear capability error for any other SQL, never a made-up result, and it reads
at most eight days of events in one query. It is what serves demos when tripl's
database role may not create databases or roles (see [managed
PostgreSQL](../run/configuration.md#demo-warehouse-on-managed-postgresql)).

The data source wears a **Demo warehouse** badge; hovering it on the in-memory
engine says why. The platform admin sees the warehouse in **Settings → Instance →
System**: *PostgreSQL · tripl_demo* with how far it is written, its size against
the cap and the days it keeps, *In-memory engine* with the reason, or *Write
failed* as an item that needs attention.

### The calendar

The model runs on the global UTC clock, never on a demo's creation time, so every
demo on the instance sees the same incident on the same day, and a demo created
at any hour opens with the newest incident already in its inbox. Something
fires every day. The tables below are generated from the model's own constants,
so they cannot describe a calendar the warehouse does not run.

<!-- demo-calendar:begin (generated by backend/scripts/render_demo_calendar.py) -->

#### Daily incidents

One incident a day, from 13:00 UTC, in this rotation (day *n* since the calendar's epoch takes kind *n* mod 5; each kind cycles through its targets):

| Kind | Scope | Targets | Lasts | Change |
| --- | --- | --- | --- | --- |
| Spike | event | `Search Performed`, `Meal Planner View`, `Recipe Saved` | 2 h | ×3, on iOS |
| Drop | event | `Recipe Detail View`, `Search Results View`, `Paywall View` | 3 h | to 15 % |
| Error burst | event | `API Error` | 2 h | ×5 |
| Event-type dip | event type | `search`, `meal_plan`, `content` | 3 h | to 35 % |
| Platform shift | event | `Home Screen View` | 3 h | ×4, on web |

#### Releases

A release every 14 days at 10:00 UTC, after a 5-day beta at 0.4 % of traffic; half the users have updated 60 hours after release, and 5 % stay behind. Each release breaks one event, in this rotation:

| Release | Event | What breaks |
| --- | --- | --- |
| *n* ≡ 0 (mod 6) | `Share Button Click` | missing in the new version |
| *n* ≡ 1 (mod 6) | `Filter Applied Click` | keeps 40 % of its volume in the new version |
| *n* ≡ 2 (mod 6) | `Video Play Click` | missing in the new version |
| *n* ≡ 3 (mod 6) | `Save Recipe Click` | keeps 40 % of its volume in the new version |
| *n* ≡ 4 (mod 6) | `Plan Toggle Click` | missing in the new version |
| *n* ≡ 5 (mod 6) | `Add to Shopping List Click` | keeps 40 % of its volume in the new version |

Every other release also sends `properties.results_count` as a string instead of a number on iOS: the property drift.

#### Recurring changes

| What | When | Shows up as |
| --- | --- | --- |
| *Weekly promo email* on `Paywall View` (×2.5) | every Tuesday, 10:00 UTC, 2 h | an anomaly marked *expected* |
| The web redesign test moves screen views to the web app (`platform`) | 4 days of every 14, from day 6 of the cycle | a change in the `platform` split |
| A TV feature in Brazil skews sign-ups and app opens to BR (`country`) | 3 days of every 14, from day 1 of the cycle | distribution drift |
| `app_heartbeat_v1`, a name no plan documents | every 3 days (day ≡ 1), 06:00 UTC, 2 h | a shadow event candidate |
| `legacy_deeplink_open`, a name no plan documents | every 7 days (day ≡ 4), 20:00 UTC, 1 h | a shadow event candidate |

<!-- demo-calendar:end -->

Each event also keeps a steady platform mix of its own, and its volume is split
by app version over the whole history, so the **By version** tab and the volume
chart cover the same days: a new version runs as a small beta for a few days,
then takes over along an S-curve while the one before it fades and older ones
keep a long tail.

### How a demo stays current

A demo is advanced the way any project is: its scan's ordinary scheduled
collection reads the warehouse. Two rules keep the cost down on an instance
hosting many demos:

- a demo collects **at most every six hours** (counted from its newest scheduled
  collection or its creation, whichever is later), because most of a collection's
  cost is detection over dozens of series. The day's incident is detected on the
  next run within a few hours; **Run now** and **Replay** collect on demand.
- a demo nobody has opened for **six hours** pauses, and resumes on your next
  visit. Nothing is lost: the warehouse keeps every hour of its retention, so the
  first collection after a pause catches the gap up from real rows.
  `tripl doctor --include-demo` does not report a paused demo's collection as
  stale.

The catalog metrics are collected by their own scheduler and keep their daily
cadence. An operator can stop demos advancing with
[`DEMO_RUNTIME_ENABLED=false`](../run/configuration.md#demo-workspace): a demo
then keeps what provisioning built, and **Run now** and **Replay** still work.

## Semantic search

The demo bundles precomputed embedding vectors for its own content and a few
suggested queries (try `purchase funnel` or `money back` in the command palette),
so smart search ranks by meaning and marks semantic matches even on an instance
with **no embedding provider configured**. Each vector is indexed both by its
exact text and by the entity it describes, so a scan that rewrites a document's
details (observed values replacing a `${variable}` template, say) keeps its
vector. No API key is used and no text leaves the instance; the vectors ship
with the release. An instance that does configure a provider embeds the demo
like any other project, so any query ranks by meaning, not only the suggested
ones ([AI and search configuration](../run/ai-and-search.md)).

## What is preview‑only / local‑simulated

- **Alerting.** Rules evaluate the pipeline's real signals and render real
  messages, and rules, the replay simulator, deliveries and the Inbox are all
  explorable — but delivery is **recorded locally and simulated**. Nothing is
  ever sent to Slack, Telegram, email, a webhook, Jira, Linear, PagerDuty, or
  Microsoft Teams, and the UI labels these as local simulated deliveries (never
  a real send success). A demo project is zero‑egress by construction: the API
  refuses to create any destination on it other than the local sink, so a demo
  can never be pointed at a real channel — connect one from a **real** project
  instead. The **flaky** sink fails the first attempt at every delivery with a
  labelled simulated error, so the failed‑delivery state and the **Retry**
  action are real: retrying re‑dispatches down the normal path and succeeds.
- **Implementation tickets and AI features** are surfaced with a clear next step
  rather than performing an external action.

## What is intentionally unavailable

- **External sends** of any kind from demo data.
- **Writing to the warehouse**, or reading anything on the server besides its two
  tables.
- **Arbitrary SQL on the in-memory engine** — it answers the demo's own query
  shapes and returns a clear capability error otherwise.
- **Attaching the demo source to a real project**, or converting it into a real
  connection. Only its display name can be edited: its edit dialog says *Demo
  sources read built-in sample data; there is no connection or timeout to
  configure.*

## Demos built by an older version

A demo built by demo data version 4 or earlier read a private, in-memory
*synthetic* source whose history was written straight into tripl. That source no
longer advances, so such a demo is frozen: its banner says *This demo was built
by an older version of tripl, so its data no longer updates*, and **Reset**
rebuilds it on the demo warehouse. Until then it keeps its data, wears a
**Synthetic** badge, and the daily prune still bounds it.

## Finding your way around

The demo offers two guides, and they do different jobs.

- **The product tour** (the **Quick overview** stepper in the *Tour &amp;
  chapters* dialog — opened from the demo bar or the welcome panel) walks the surfaces:
  Events, Scans, Overview, Metrics and fact tables, Alert rules, Anomalies,
  Coverage, Reconciliation, Branches and the alert preview, and ends by opening
  *Search by meaning* (the command palette) for you. Opening a step's surface
  advances the tour and it remembers where you were, so it can be followed across
  navigations instead of restarting every time. **Open &lt;surface&gt;** leaves
  the tour docked as a small card on that page — *Tour · step N of 11*, the
  page's name, **Next: &lt;next page&gt;**, **All steps** to reopen the dialog,
  and **Close the tour**, which keeps your place. The card hides while a
  chapter is running. **All surfaces** at the bottom
  of the tour expands a direct index of every surface and metric building block.
- **The coached chapters** (**Start: &lt;chapter&gt;** on the welcome panel, or
  **Hands-on chapters** at the top of the *Tour &amp; chapters* dialog) each make
  one thing happen end to end. The first,
  **Run the live loop**, is the core:

  1. **Run a scan** — from any scan's *Run now*.
  2. **Watch it land** — the run completes and shows what it changed.
  3. **Collect a metric** — from *Collect now* on any metric.
  4. **See the chart move** — open that metric and see the series your
     collection recomputed (a daily metric gains a new point only once a day
     has closed).

  The other chapters walk one area apiece: **Edit an event** (on `Trial Started`,
  replace the current Product ID value with `prod_monthly`; the guide advances
  automatically, then asks you to type `$`, select `${product_id}`, and save),
  **Properties & value drift**, **Review a branch** (up to the merge preview —
  merging stays your call), **Reconcile the plan**, **Route an alert** (against
  the local demo sink), and **Explore the rest**.

  A strip joined to the bottom of the demo banner — one bar, not two cards —
  tracks which chapter and step you are on and links to where the next action
  lives. The link hides when you are already on that page, or when the control
  is already on it. When the bar is narrow (the activity panel open beside the
  page), its buttons drop to icons, then the progress dashes, the instruction and
  the chapter name go, so the step title stays readable.

  On the page, the step's control — the exact button or input that performs it —
  wears a pulsing ring and a small tag pointing at it, such as **Click here**,
  **Type here** or **Open this menu** (on a touch screen the guide says **Tap**
  instead of **Click**). The ring is clipped to what can be seen of
  the control, and the ring and tag step aside while something else, such as a
  dialog or a menu, covers the control. When the control opens a menu or a
  dialog of its own, the ring moves on to what you click next in it — **Collect
  now** in the metric's menu, **Replay** in the rule's, **Create** in the new
  rule, **Accept** on the schema drift, the `${product_id}` suggestion under
  **Product ID** — and back to the control if you close it. Once you have made
  that second click, the ring waits for the answer instead of asking for the
  menu again, and comes back only if the request fails.

  The **demo guide** — a small card with a friendly face, in a corner of the
  window — says what the step is for and what to do next. It takes a bottom corner —
  the emptier of the two — that covers neither the control nor an open dialog,
  menu or notification, and moves to the top only while both bottom corners
  would, so it never sits over a page’s title or toolbar for want of a quieter
  spot. It stays in its corner until something needs it. Pressing the
  guide's own buttons never closes the dialog, menu or suggestion list it is
  coaching. When a large dialog leaves no corner
  of the page free, a narrower card takes the edge of the screen beside the
  dialog, so the step stays readable while you work in it — on a 1280-pixel
  screen even beside the widest, the property and rule replay dialogs, where
  the card leaves its face out to keep room for the words. Only when there is no
  room even for that does it fold into a round button showing the step number —
  click it to read the step. You can minimize the card yourself at any time. On
  a phone it
  spans the width of the screen, at the bottom, or at the top while the control
  is down there. On a phone the open card leaves its face out, to take less of the
  screen. The page gets room underneath the card, so nothing ends up
  stuck behind it.

  When the control is not on the page you are on, the guide says where it is:
  it rings the sidebar item that leads there (its tag sits under the item, inside
  the sidebar) — or, when the step lives in another
  section of the page you are on (Alerting's **Rules**), the tab that opens it —
  and offers **Take me there**. If
  you leave a step halfway (closing the event editor, for instance), the ring
  moves to the way back (the event's edit button). When the control should be on
  the page and is not, the guide says so, and adds that resetting the demo
  project restores every guided example if you are allowed to reset it; a viewer
  is told the step needs edit access instead.

  Screen readers hear the step's instruction as the control's description.
  **Hide hints** — on the guide, or in the strip — quiets the ring, the tag and
  the guide for the rest of the browser session on that project. The guide's
  face stays in its corner: click it, or **Show hints** in the strip, to bring
  them back. All of it is demo-only and never appears in a real project.

  When a chapter is complete, the guide says so and offers the next one —
  **Start the chapter** takes you to its first page with its first step live.
  After the last chapter it points out of the demo: **Create a real project** on
  your own instance, which opens **All projects** with the **New project** dialog
  already open, or **Run tripl yourself**, the
  [Quick Start](../quick-start.md), on a public demo, which has no projects of
  your own to create. The strip offers the same.

  The chapters follow **your** actions, not the demo's. The runtime clock is
  producing real scans and collections of its own in the background, so a step
  advances only when the run or collection **you** started settles — a background
  run never ticks it forward. A run that fails, or that a reset wipes out, sends you
  back to the action with an explanation rather than leaving you waiting. The
  current Scans page also follows that exact run to its terminal status, even if a
  realtime update is missed, so completing the step never requires a reload.
  Progress is remembered per project (in your browser), so reloading mid-scan
  resumes the watch, and two tabs on the same demo keep each other's progress.
  A viewer, who cannot run scans or edit, sees "This step needs edit access…"
  in place of the action.
  On your first visit to the Overview the welcome panel stands in for the strip;
  the strip appears there once you start a chapter or put the panel away.

  Dismiss it at any point — including after finishing. The welcome panel is a
  single row, so the Overview leads with the product rather than with
  onboarding: a line saying how many short chapters there are and that the
  guide points at every click, **Start: &lt;chapter&gt;** (or **Continue: &lt;chapter&gt;** once you
  have begun), **Tour &amp; chapters**, which opens the same *Tour &amp; chapters*
  dialog as the demo bar’s button — **Hands-on chapters** first, then the
  **Quick overview** stepper — and **Create a real project**, which opens the New
  project dialog (on a public demo, where there is none to create, **Run tripl
  yourself** opens the Quick Start instead). Dismissing the panel outright (the **✕**) hides it
  for that project and offers **Undo** for a few seconds. After that, the demo
  bar's **Tour &amp; chapters** button — present on every demo surface, for
  everyone — opens the same dialog, and the tour offers **Show the welcome panel
  on Overview** while the panel is hidden.

## Lifecycle

- **Create** — provisioning is atomic: you either get a fully‑ready demo or a
  clean failure, which never leaves a half‑built project in your list.
  The request only reserves the project; a background worker seeds it, and
  the dialog waits until the project reads ready. That keeps the app
  responsive for everyone while many demos are created at once — they queue on
  the worker instead. An instance that keeps a pool of demos seeded ahead of
  time hands you one of those at once. It takes about half a minute; the
  creation dialog (*Generating demo project*) lists the four stages — *Writing
  the tracking plan*, *Scanning the demo warehouse*, *Collecting history* (the
  last two days, with how far through it is) and *Detecting signals* — and
  marks the one the server reports. It says so when a create runs past two
  minutes, and stops waiting after five. The older history is filled in after
  the demo opens; the banner shows *Filling in history* until it is done.
  On a public demo a newcomer does not have to find the button: signing in to
  an organization with no projects of their own starts the create, once per account in that
  browser. After a cancel, or with every demo deleted, the button waits for
  them instead.
  A failure says what is actually known: the server's own failure was rolled
  back and can be retried; a demo limit or a refusal says why and offers no
  retry; a lost connection or a timeout may still have created the demo, so
  the dialog offers no **Try again** — check the projects list first.
- **Cancel** — closing the creation dialog, pressing Escape, or clicking Cancel
  asks the server to abandon the provision, not just the browser to stop
  listening. If it is still seeding, the project is discarded and nothing is
  added to your projects or left behind in the audit log. If the demo finished
  just before the cancel arrived, the dialog says it is in your list — delete it
  from its banner's **Manage demo** menu if you do not want it. If the server had nothing left to
  cancel at all, the dialog says only that.
- **How many** — you can hold up to **three** demo projects at a time. At
  three, **Generate demo project** is disabled with the reason beside it: reset
  or delete one first from its banner's **Manage demo** menu. Beyond the first, generating another asks for
  confirmation and points at Reset; each
  extra demo is named `Demo Project 2`, `Demo Project 3`, … so they are
  distinguishable in the projects list. A new demo takes the lowest name you
  are not already using, so after deleting `Demo Project` the next one is named
  `Demo Project` again rather than repeating a name you still have.
- **Reset** — chosen from the banner's **Manage demo** menu; re‑seeds the demo in place under the same URL, preserving ownership
  and its name. Reset re‑runs the current recipe, so it is also how you refresh a
  demo built from an older one. It re‑seeds everything in one transaction and
  takes about as long as a create; a progress dialog lists the same stages. If the
  reset has not answered after five minutes the page stops waiting and says the
  server may still be re‑seeding. It leaves for the Overview and drops the
  cached data and branch selection at once, then keeps checking for a few
  minutes: when the re‑seeded demo appears it finishes the reset as usual
  (fresh chapter progress, welcome panel back). Reset and Delete stay off in the
  **Manage demo** menu while it checks.
- **Delete** — chosen from the banner's **Manage demo** menu; removes the demo, its
  plan, history and data source, and leaves every real data source untouched. The
  shared demo warehouse stays for the other demos. The creator or an owner can delete it. A demo
  is visible only to its creator (and the organization's owners and admins) unless they add members in
  **Settings → Project → Access**; a reset keeps those members. The
  tour position, chapter progress and welcome/hint choices your browser kept
  for it are cleared too — and the workspace page clears them for any demo that
  was deleted elsewhere, the next time it lists your projects.
- **Demo data version.** Each demo records the version of the demo data (the seed
  recipe) it was built from, listed under **What’s simulated** on the demo
  banner. Reset rebuilds it from the current version.
- **Failed shells.** A demo whose seed failed leaves a hidden, non-listable
  project row behind as a diagnostic marker. Those are reclaimed automatically:
  the next demo creation on the instance deletes any that are more than a week
  old.

## Turning the demo off for a deployment

Three operator switches control the feature, all documented in the
[Configuration reference](../run/configuration.md#demo-workspace):

- **`DEMO_ENABLED=false`** turns demo **provisioning** off. That blocks
  **Create** *and* **Reset** — a reset re-seeds a demo from scratch, so it
  provisions one too — and both answer `403 Demo provisioning is disabled`.
  **Delete** deliberately stays available, so an organization can never be stuck with
  a demo it cannot remove.
- **`DEMO_RUNTIME_ENABLED=false`** stops demos advancing: their scheduled
  collection is never dispatched, so a demo keeps what provisioning built and
  stays fully usable, with **Run now** and **Replay** still collecting on demand.
  The warehouse itself keeps being written, so a demo created later is current.
- **`DEMO_WAREHOUSE_MODE=memory`** keeps every demo on the in-memory engine and
  the warehouse writer idle.

None of them affects real projects.
