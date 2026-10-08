---
title: Start here
sidebar_position: 0
---

# Start here

*For the people who live in the numbers — product managers, analysts, growth,
data. Nothing here assumes you write code.*

## The problem, as you actually meet it

It's Tuesday. You open the funnel dashboard and checkout conversion is down 18%
week over week. You spend the afternoon asking the obvious questions: did we ship
something? Is it a real drop, or did we stop *measuring* it properly?

Three days later someone finds it. Last week's Android release renamed a field,
and `checkout_completed` has been arriving without its `payment_method` since
Thursday. Conversion never moved. The dashboard was just wrong, quietly, for six
days — and every decision made from it in that window was made on bad numbers.

Nobody was careless here. This is simply what happens when the description of
what you're supposed to be tracking lives in a spreadsheet, a Notion page and
three people's heads, while the actual data lives somewhere else entirely and
nothing compares the two.

That comparison is the whole job tripl does.

## What tripl is, in one breath

**A written-down tracking plan, checked continuously against the events really
landing in your warehouse, with a message when the two stop agreeing.**

It reads the analytics data you already have — ClickHouse, BigQuery,
Databricks, Snowflake, Redshift, Greenplum or PostgreSQL. There's no SDK to ship, no re-instrumentation, and nothing your app
has to send anywhere new. If your events already land in a warehouse, tripl can
start telling you the truth about them this week.

## The three things it does

**It writes the plan down.** Every event, the fields it carries, what the values
are allowed to be, who owns it, whether it's live or deprecated. One place,
searchable, that an engineer and an analyst can both point at during a
disagreement.

**It checks the plan against reality.** tripl reads your warehouse on a schedule
and compares: events you documented but that never arrive, events arriving that
nobody documented, fields that quietly changed shape. You get a coverage number
that means something, instead of a feeling.

**It tells you when the numbers move.** Not "here's a chart, go look" — an actual
message, in Slack or Telegram or email, when an event drops or spikes beyond what
its own history says is normal. It learns each event's rhythm, so a quiet 3am and
a slow Sunday don't page you.

## What a week looks like

**Monday.** You open the project and the Overview shows one open signal:
`add_to_cart` on iOS dropped 40% yesterday afternoon. You click it, see the
chart, and recognise the shape — it starts exactly when the 4.2 release rolled
out. You mute the signal for a day, note the release, and ping the mobile team
with something specific instead of "analytics looks weird".

![An event with an open spike: the chart, the signal, and which platform the change came from](/img/screenshots/event-monitoring.light.webp#gh-light-mode-only)
![An event with an open spike: the chart, the signal, and which platform the change came from](/img/screenshots/event-monitoring.dark.webp#gh-dark-mode-only)

**Wednesday.** A PM asks whether you already track "user removed an item from
the basket". You press `⌘K`, type "remove basket item", and find
`cart_item_removed` — documented eight months ago, live, with the fields it
carries. Two minutes instead of two days and a duplicate event.

![Search or jump: typing finds events, tags, branches and pages](/img/screenshots/command-palette.light.webp#gh-light-mode-only)
![Search or jump: typing finds events, tags, branches and pages](/img/screenshots/command-palette.dark.webp#gh-dark-mode-only)

**Thursday.** You're planning next quarter's checkout revamp. You open a
**branch** of the plan, draft the four new events with their fields, and share
it for review. Nothing touches the live plan until it's approved and merged —
so the debate happens on a proposal, not on production.

![A plan branch with one change, waiting to be submitted for review](/img/screenshots/branches.light.webp#gh-light-mode-only)
![A plan branch with one change, waiting to be submitted for review](/img/screenshots/branches.dark.webp#gh-dark-mode-only)

**Next Monday.** The weekly digest lands: coverage moved up, two deprecated events
finally stopped firing, and one undocumented event showed up that nobody claims.
You add it to the plan, or you go find out who's sending it.

## Where you fit, and where engineers do

You don't need an engineer to use tripl day to day. You do need one once, at the
start.

| Someone technical does this once | You do this continuously |
| --- | --- |
| Runs the app, or a colleague hosts it | Write and maintain the plan |
| Connects the warehouse (read-only credentials) | Review what scans find |
| Sets up the first scan | Tune what's worth alerting on |
| | Act on signals, own events, propose changes |

Connecting a warehouse is genuinely a technical step — it needs credentials and
someone who knows which tables the events land in. Everything after that is
yours.

## What it deliberately doesn't do

Being straight about this saves you an evaluation:

- **It is not an analytics SDK.** It doesn't collect events; it reads events
  you're already collecting.
- **It is not a BI tool.** It won't replace your dashboards. It tells you when
  to distrust them.
- **It doesn't fix your data.** It finds the gap between intent and reality and
  puts a name on it. Closing that gap is still a conversation with the team that
  owns the code.
- **It won't guess your plan for you.** A scan gets you a first draft of the
  catalog from what's really arriving — the descriptions, ownership and intent
  are yours to write.

## Try it before you commit to anything

There's a **demo workspace**: one click, no warehouse, no credentials. It seeds a
realistic project with events, history, live-looking anomalies and a worked
alerting example, so you can click through the whole product before deciding
whether to point it at your own data.

See [The demo workspace](./demo-workspace.md).

When you create your own project, you can start it from an industry template
(e-commerce, subscriptions, mobile games or B2B SaaS). The template's starter
plan opens as a draft branch for you to review, and your live plan stays empty
until you merge it. See [Project templates](./project-templates.md).

## Where to go next

- **[How-to guides](../how-to/index.md)** — one task per page, with
  screenshots: connect a warehouse, set up an alert, invite the team.
- **[Concepts](./concepts.md)** — every idea in the product, in plain language.
  Read once and the rest of the docs stop being cryptic.
- **[User Guide](./user-guide.md)** — the doing version: empty screen to a
  working, tuned alert.
- **[Alerting](./alerting.md)** — how to get told, without getting spammed.
- **[How anomaly detection works](./anomaly-detection.md)** — for when you want
  to know *why* something was flagged, and how to make it more or less twitchy.

If you're the person installing it, start at the
[Quick Start](../quick-start.md) instead.
