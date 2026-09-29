---
title: Health score
sidebar_position: 12
---

# Health score

The health score answers one question per event: how much should I trust the
data this event produces today? Each event on the **main** plan gets a number
from 0 to 100, built from six components that tripl already tracks: whether the
event arrives, whether it keeps its contract, open drifts, open signals, source
freshness and documentation.

The score is computed from facts already in tripl. No language model is
involved, and the same facts at the same moment always give the same score.

For where the score appears in the app, see the
[Feature Reference](./feature-reference.md#health-score). For the API, see the
[Agent API Guide](../integrate/agent-api-guide.md#health-score).

## Which events are scored

Only events of the **main** plan whose status is not `archived` are scored.
Scans write metrics, drifts and lifecycle findings for main rows only, so a copy
of an event on a working branch has nothing of its own to score. On a branch the
catalog has no Health column and no **Least healthy first** sort, and
`order_by=health` on a branch answers `400`.

## Components and weights

The weights are fixed. In v1 they are **not** configurable per project: they
live in one module (`backend/src/tripl/services/health_weights.py`) as named
constants, and this table matches it.

| Component | Weight | Value (0 to 1) |
| --- | --- | --- |
| Implemented & seen | 25 | `implemented` / `live`: 1 if the event was last seen at most 7 days ago, 0.5 if at most 30 days ago, 0 if older or never seen. `deprecated`: 0 with an open *sunset overdue* finding, else 0.5 with an open *successor silent* finding, else 1. |
| Contract | 20 | 1 − (failing rules / all rules). |
| Drifts | 15 | 1 − 0.25 × open drifts, never below 0. |
| Signals | 15 | 1 − 0.5 × open signals that need a verdict, never below 0. |
| Freshness | 10 | The worst freshness among the scans covering the event: fresh 1, late 0.5, overdue 0. |
| Documentation | 15 | 0.5 if the event has a description, plus 0.5 if it has an owner. |

What each component counts:

- **Contract rules** are the rules of the event's event type that a scan checks:
  one per required field, per `enum` field with options, per field with a regex,
  and per field with a minimum or maximum. A rule is failing while an active
  schema drift of the matching kind (required, enum, regex, range) is open on
  that field.
- **Drifts** adds four counts: active structural schema drifts on the event
  type (new field, missing field, changed type; contract violations are not
  counted here again), active value drifts on the event itself, active
  [property drifts](./variables-and-templates.md#property-drift) on the event
  itself (a new property or a missing required one; a type change is about a
  property rather than an event and is not charged to any event), and the
  number of fields of the event type with a *significant* distribution drift in
  the last 7 days. Schema and distribution drifts are recorded per event type,
  so every event of that type carries them. Each costs the same 0.25. The
  property-drift count is the same number, by the same rule, as the project's
  open property drifts on the sidebar and in the project summary.
- **Signals** counts the event's open, significant event-scope signals that
  have no verdict yet: the same signals the Monitoring badge counts. Any
  verdict removes a signal from the count (including a mute or *expected*), and
  so does a verdict on the incident the signal was routed to. *Acknowledged* is
  not a verdict, so an acknowledged signal still counts.
- **Owner** means an owner on the event or at least one owner on its event type.
  A description made only of spaces does not count.

## Excluded components

A component that does not apply to an event is **excluded**, and the remaining
weights are renormalized over their own sum. An event is never marked down for
something it cannot have:

| Excluded component | When | Reason shown |
| --- | --- | --- |
| Implemented & seen, Contract, Drifts, Signals, Freshness | The event is planned: `draft`, `in_review` or `ready_for_dev`. A planned event is scored on documentation only. | *Not implemented yet* |
| Contract | No scan covers the event: violations are found only by a scan, so nothing has checked the rules. | *Not covered by any scan* |
| Contract | A scan covers the event, but its event type has no contract rules. | *No contract rules on this event type* |
| Drifts | No scan covers the event. | *Not covered by any scan* |
| Signals | No scan covers the event, or anomaly detection is off on every scan that covers it. | *Anomaly detection is off for its scans* |
| Freshness | No scan that covers the event runs on a schedule (a scan with no interval or no time column has unknown freshness and is ignored). | *No scheduled source* |

Documentation always applies.

A scan **covers** an event when it wrote a metric for the event in the last 30
days, or when it is bound to the event's event type.

## How the score is computed

Over the components that apply:

```text
score = round(100 × Σ(weight × value) / Σ(weight))
```

Rounding is half up, and the result is kept between 0 and 100. Grades:

| Grade | Score |
| --- | --- |
| healthy | 80 and above |
| warning | 50 to 79 |
| unhealthy | below 50 |

The breakdown lists every component with its fixed weight, its **effective
weight** (its share in percent after renormalization), its value, the
**points** it contributes (effective weight × value) and a one-line reason. The
points of an event add up to its score, give or take rounding. The **main
issue** shown next to a score is the reason of the component that loses the most
points; on a tie the heavier component, in the table order above, wins.

### Worked example

A `live` event last seen 10 days ago. Its event type has 4 contract rules and
one of them is failing. One scan covers it: that scan has one open schema drift,
its freshness is late, and anomaly detection is off. The event has a
description but no owner.

| Component | Weight | Value | Effective weight | Points |
| --- | --- | --- | --- | --- |
| Implemented & seen | 25 | 0.5 | 29.4 | 14.7 |
| Contract | 20 | 0.75 | 23.5 | 17.6 |
| Drifts | 15 | 0.75 | 17.6 | 13.2 |
| Signals | excluded | | | |
| Freshness | 10 | 0.5 | 11.8 | 5.9 |
| Documentation | 15 | 0.5 | 17.6 | 8.8 |

The weights that apply sum to 85, so the score is
`round(100 × 51.25 / 85) = 60`: **warning**. Implemented & seen loses the most
points (14.7), so the main issue is *Last seen 10d ago*.

A planned event with a description and no owner is scored on documentation
alone: 50, **warning**, main issue *No owner*.

## Event types and the plan

An event type's score and the project's score are the mean of the scores of
their scored events, rounded half up; an event type with no scored events has
no score. Both also show how many events are healthy, warning and unhealthy,
the average value of each component over the events it applies to, and the five
least healthy events (lowest score first, then by name).

## Trend and weekly digest

Every day at 05:55 UTC tripl stores a snapshot of each project's plan score, and
keeps snapshots for 400 days. The **Plan health** panel on the Overview draws
its trend from these snapshots and compares today's score with the snapshot
from 7 days earlier.

The Monday weekly digest reads the latest snapshot, never a live score. It adds
a line such as `- Plan health: 72/100 (-3 vs last week)` and a **Least healthy
events** list with the five lowest events and the main issue of each. When the
latest snapshot is more than two days old, or it scored no events, the digest
leaves the health lines out rather than print a stale number.

## Freshness of the numbers

Event scores are computed when you ask for them. The project summary behind the
Overview panel is cached for up to two minutes. The trend changes once a day.
