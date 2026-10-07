---
title: How-to guides
sidebar_label: Overview
sidebar_position: 0
description: Short, task-sized guides with screenshots. Pick the job in front of you and follow the steps.
---

# How-to guides

Each guide here does one job, start to finish, in a few minutes of reading. They
show the screen you will be looking at and tell you what to click, what to type
and what you should see afterwards. When you want the full detail behind a step,
every guide ends with a link to the reference page for it.

![The Overview page of a tripl project: open signals, coverage, volume and the busiest events](/img/screenshots/overview.light.webp#gh-light-mode-only)
![The Overview page of a tripl project: open signals, coverage, volume and the busiest events](/img/screenshots/overview.dark.webp#gh-dark-mode-only)

## Your first hour

1. [Try tripl on the demo project](./explore-the-demo.md): see everything working
   on realistic data, with nothing to connect.
2. [Connect your warehouse](./connect-your-warehouse.md): point tripl at
   ClickHouse, BigQuery, Databricks, Snowflake or PostgreSQL. It only ever reads.
3. [Draft your tracking plan from a scan](./draft-your-plan-from-a-scan.md): turn
   the events already in your warehouse into a written plan.
4. [Get a message when the numbers move](./get-alerted.md): send alerts to Slack,
   Telegram, email or a ticket tracker.

## Every week

- [Investigate a signal](./investigate-a-signal.md): a chart moved; find out why
  and record what it was.
- [Add or change an event](./add-or-edit-an-event.md): describe a new event, or
  fix the description of an old one.
- [Propose plan changes for review](./propose-changes-on-a-branch.md): change the
  plan on a branch and merge it after someone has looked.
- [Find gaps between the plan and the data](./find-gaps-between-plan-and-data.md):
  events nobody documented, and documented events that stopped arriving.

## Your team and your tools

- [Invite your team](./invite-your-team.md): add people to the organization and
  to a project, as editors or viewers.
- [Keep team notes next to the plan](./keep-team-notes.md): write down what the
  plan cannot say, and translate it.
- [Let an AI agent read the plan](./connect-an-ai-agent.md): give Claude or any
  MCP client a key and the plan to work from.

:::tip Not sure what a word means?
[Concepts](../use/concepts.md) explains every idea in tripl in plain language:
events, event types, scans, signals, branches.
:::
