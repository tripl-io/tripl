---
slug: /
title: tripl
sidebar_label: Overview
sidebar_position: 1
---

# tripl

**Keep your product analytics honest.**

tripl is the single place where your team writes down what you *intend* to
track, checks it against what your apps are *actually* sending, and gets a
heads-up the moment the numbers start to look wrong.

tripl works with the analytics data you already have. It connects to your
existing data warehouse — **ClickHouse**, **BigQuery**, **Databricks**, **Snowflake**, **Amazon Redshift**, **Greenplum**, or **PostgreSQL** — and
reads the events that are already landing there. There's no new SDK to ship and
nothing to re-instrument.

![A tripl project's Overview: open signals, plan coverage, a week of volume and the busiest events](/img/screenshots/overview.light.webp#gh-light-mode-only)
![A tripl project's Overview: open signals, plan coverage, a week of volume and the busiest events](/img/screenshots/overview.dark.webp#gh-dark-mode-only)

## Start with whichever of these you are

**"I work with the numbers."** Product manager, analyst, growth, data — you want
to know what this does for you and what a week with it looks like.
→ **[Start here](./use/start-here)**, then
[Concepts](./use/concepts) and the [User Guide](./use/user-guide).

**"I have one thing to do."** Connect a warehouse, set up an alert, invite the
team, look into a chart that moved.
→ **[How-to guides](./how-to/index.md)**: one task per page, with screenshots.

**"I need to get it running."** You have Docker and half an hour, and you want a
working instance pointed at a real warehouse.
→ **[Quick Start](./quick-start)**.

**"I run it for other people."** Deployment, upgrades, backups, configuration,
security, and what to do at 3am.
→ **[Self-hosting & Operations](./run/release)** and
[Administration](./administer/admin-guide).

**"I want to drive it from code."** Scripts, CI, or an LLM agent reading and
writing the catalog through the API.
→ **[Automation & agents](./use-cases/overview)** and the
[Agent API guide](./integrate/agent-api-guide).

**"I want to change tripl itself."**
→ **[Development](./develop/architecture)**.

Not sure yet? The [demo workspace](./use/demo-workspace) is one click and needs
no warehouse — it's the fastest way to see whether this is for you.
