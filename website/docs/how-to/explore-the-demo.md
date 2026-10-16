---
title: Try tripl on the demo project
sidebar_label: Try the demo project
sidebar_position: 1
description: Generate a fully populated demo project and see scans, monitoring, alerts and review working, with nothing to connect.
---

# Try tripl on the demo project

**You will:** see the whole product working on realistic data in about ten
minutes, without connecting a warehouse or sending anything anywhere.

**You need:** a running tripl and an account. On a fresh instance, the first
person to register becomes the owner.

## 1. Generate the project

On **All projects**, press **Generate demo project**.

![All projects, with the Generate demo project button at the top right](/img/screenshots/workspace.light.webp#gh-light-mode-only)
![All projects, with the Generate demo project button at the top right](/img/screenshots/workspace.dark.webp#gh-dark-mode-only)

tripl builds a project for an imaginary recipe app, the way a real project
starts: it writes a tracking plan of 157 events across 11 event types, then
scans the **demo warehouse** and collects the last two days of history from it.
That takes about half a minute, and the dialog shows each stage as the server
reaches it; the rest of the 23 days is filled in while you look around (the
banner says how far it is, and the weekly promo verdicts and the full set of
alerts land when it finishes, a few minutes later). You land on metrics, open anomalies from the warehouse's daily incident, value and
schema drift, release regressions, and a plan branch waiting for review.

The demo warehouse is a database of generated traffic on your tripl server,
shared by every demo, so nothing leaves the server. Everything observed in the
demo — the scans, detection, drift and alerting — is tripl's own pipeline
reading it.

## 2. Follow a chapter

The project opens on **Overview** with a welcome panel. Press **Start: Run the
live loop**.

![The demo project's Overview, with the demo banner and the welcome panel](/img/screenshots/demo-welcome.light.webp#gh-light-mode-only)
![The demo project's Overview, with the demo banner and the welcome panel](/img/screenshots/demo-welcome.dark.webp#gh-dark-mode-only)

A chapter is a short hands-on lesson. A strip under the banner names the next
step, a ring and a small tag mark the button to press, and the demo guide in a
corner of the window says what to do and where to go. The first chapter runs the
loop the whole product is built on: run a scan, watch it land, collect a metric,
see the chart move. **Tour & chapters** lists the others: editing an event,
properties and value drift, reviewing a branch, reconciling the plan, routing an
alert.

Chapters move forward only when *you* act. The demo also runs scans on its own
in the background, and those never skip a step for you.

## 3. Look around on your own

A good order, once the first chapter is done:

| Open | What you will find |
| --- | --- |
| **Plan → Events** | The catalog: every event, its status, its fields and how busy it is. |
| **Observe → Overview** | The state of the project on one screen. |
| **Observe → Anomalies** | The moves detection found, waiting for a verdict. |
| **Observe → Alerting** | The incidents alert rules raised, and what was sent. |
| **Govern → Reconciliation** | Events in the data but not in the plan, and the reverse. |

Click an event with a red dot to open its monitoring page: the volume chart, the
incident, and *why it changed*, broken down by platform. The warehouse runs one
incident a day at 13:00 UTC, and a release every two weeks that breaks one
event, so there is always something recent to look at.

![An event's monitoring page: volume chart with a spike, the signal and the breakdown of what moved](/img/screenshots/event-monitoring.light.webp#gh-light-mode-only)
![An event's monitoring page: volume chart with a spike, the signal and the breakdown of what moved](/img/screenshots/event-monitoring.dark.webp#gh-dark-mode-only)

## When you are done

Reset or delete the demo from **Manage** on its banner; it never touches your
other projects. When you are ready for your own data, go back to **All
projects**, press **New project**, then
[connect your warehouse](./connect-your-warehouse.md).

**More detail:** [The demo project](../use/demo-workspace.md) lists what the
recipe writes and what the pipeline observes, the calendar of incidents the
warehouse runs, and what the demo deliberately cannot do (such as send a real
Slack message).
