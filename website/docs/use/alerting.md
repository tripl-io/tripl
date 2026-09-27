---
title: Alerting rules
sidebar_position: 6
---

# Alerting rules

Alerting turns the anomaly and drift **signals** tripl finds during a scan into
notifications and tickets. You configure it per project on the **Alerting** tab
(**Observe → Alerting**). Any member can read alerting config; creating,
editing, retrying, or muting requires the **editor** or **owner** role.

The model has three layers:

**Destination** (a channel) → **Rule** (routes matching signals to one
destination) → **Delivery** (a single send attempt, carrying the matched items).

A rule lives under a destination, and a destination belongs to a project, so by
default a rule evaluates the signals produced by **every scan in the project**.
A rule can also be **narrowed to a single scan** with the **Scan** picker in the
rule editor — see [Narrowing a rule to one scan](#narrowing-a-rule-to-one-scan).

:::note Demo projects are zero-egress
In a generated demo project the only destination that can exist is the local
**demo sink**: the API refuses to create a Slack, Telegram, webhook, email, Jira,
or Linear destination there, and every delivery is rendered and recorded locally
rather than sent — the UI labels those rows as simulated, never as a real send.
That local sink never fails on its own, so the demo deliberately seeds one
**failed** earlier attempt at the same incident: the failed-delivery state and
the **Retry** action below are both reachable without leaving the demo, and
retrying re-dispatches down the normal path and succeeds. See
[The demo workspace](./demo-workspace.md).
:::

## Where signals come from

A rule never invents an alert — it reacts to **signals** the anomaly detector
produces on each scan. In short: for each scope the detector compares the latest
bucket against a seasonal baseline and scores the gap as
`z = (actual − expected) / spread`, recording a **spike** or **drop** when
`|z| ≥ sigma_threshold` (default 4) and the expected volume clears
`min_expected_count` (default 50). It also emits **distribution-drift** signals
(a value mix shifted) and **release-regression** signals (a new app version
under-fires an event), plus **variable-value drift** when an event observes
values outside its effective documented variable list. A scan whose source is
late or overdue produces one **source freshness** signal instead of a drop on
every scope. See [Source freshness](#source-freshness). A daily lifecycle check
adds **lifecycle** signals for retirements that are not going to plan — see
[Lifecycle](#lifecycle).

The full math — seasonal vs rolling baselines, the robust spread and its floor,
the PSI drift score, and the release-regression test — is in
**[How anomaly detection works](./anomaly-detection.md)**. The rule controls below
are an **additional** filter on top of that detection.

## Destinations

A destination is where alerts go. Each has its own connection settings and the
message formats it supports.

The **Destinations** panel is a flat list, one card per destination with its
channel's icon, and an **Add destination** menu that picks the channel. The
destination dialog has no channel select of its own: on create the channel is
the one the menu item named, and on edit it is shown read-only. A stored
secret shows as **Configured** rather than its value, and **Delete** is an icon
button inside each card (named *Delete destination &lt;name&gt;* for screen
readers).

| Channel | Key settings | Formats |
|---------|--------------|---------|
| **Slack** | Incoming webhook URL (must be a `hooks.slack.com` HTTPS hook) | plain, Slack `mrkdwn` |
| **Telegram** | Bot token + chat ID (numeric, or `@channel`) | plain, HTML, MarkdownV2 |
| **Webhook** | HTTPS target URL (SSRF-guarded) + one optional custom header | plain JSON |
| **Email** | Up to 50 recipients, optional From / subject; uses the instance SMTP settings | plain |
| **Jira** | Base URL + project key + issue type (default `Task`) | plain |
| **Linear** | API token + team, optional initial state and labels | plain |

:::note
**Jira** and **Linear** create **one ticket per delivery** (with a dedup guard so
the same delivery doesn't open duplicates). The chat channels (Slack, Telegram)
post a message; **Webhook** POSTs a JSON payload. **MarkdownV2** falls back to
plain text automatically if a message can't be rendered safely.
:::

Credentials are write-only. When you edit a destination, a secret box left empty
keeps the stored value. The webhook's custom header is a pair: a new header name
needs its value. To stop sending a stored header, use **Remove secret header**
in the edit dialog; it removes the stored secret value too. Over the API, sending
`webhook_header_name: null` in the update does the same. A null or blank
`webhook_header_value` next to a kept name keeps the stored secret.

### Delivery schedule — send now, or collect into a digest {#delivery-schedule}

By default a destination delivers **immediately**: the moment a metrics
collection finds something a rule matches, the message goes out. With
collections running every few minutes across several scans, that is a message
whenever anything is wrong — which is what you want for a pager and not what
you want for a channel people read in the morning.

**Delivery schedule** on the destination changes *when* the messages leave,
never *what* they contain. Pick a cadence and everything the rules match is
held and collected instead of sent:

| Cadence | Means |
|---------|-------|
| **Immediately** | Send after every collection. The default, and what every destination created before this option existed still does. |
| **Hourly** | One send per hour, on the hour. |
| **Daily** | One send a day, at a time you pick. |
| **Several times a day** | One send at each time you list, e.g. `09:00, 18:00`. |
| **Weekly** | One send a week, on a day and time you pick. |
| **Custom (cron)** | A 5-field cron expression — `minute hour day-of-month month day-of-week`, e.g. `0 9,18 * * 1-5` for 09:00 and 18:00 on weekdays. |

Times are read in the **project's timezone**, set on
*Settings → General → Timezone* (an IANA name such as `Europe/Moscow`; new and
pre-existing projects are `UTC`). The zone is honoured across daylight-saving
changes: "daily at 09:00" stays 09:00 local as the UTC offset shifts.

**What "collected" means, exactly.** While a destination is on a cadence, each
scope that matches a rule occupies **one line per direction**, refreshed by
every collection until the moment the digest is sent. A scope that has been
dropping all day is one line carrying its *latest* numbers, not twenty-four
lines carrying its first. A scope that **dropped in the morning and spiked in
the afternoon is two lines**, one in each of the digest's two groups — two real
movements are two incidents, and you can acknowledge them separately. Nothing is
dropped and nothing is sent twice: an alert that arrives while a digest is being
assembled simply lands in the next one.

So a digest is every incident that fired inside the window, each carrying its
own last reading — not a snapshot of what is still broken at the moment it goes
out. A scope that fired at 03:00 and recovered is still in the morning's digest,
showing the 03:00 numbers.

An empty window sends nothing at all — a quiet day is silent, not a message
saying there is nothing to report.

**You can see what is being held.** A destination card on a cadence shows a
`12 held` badge beside the schedule, and every metrics collection records
`alerts_buffered` next to `alerts_queued` in its scan job. Both exist for one
reason: while a destination is holding alerts it is silent, and silence is also
what a broken destination looks like. These say which one it is.

**A digest reads like a digest.** It is laid out for triage rather than for
interruption: one line per alert, the event name carrying the link so no raw
URL takes up the line, and the items grouped.

- **Drops come first.** A drop needs a baseline to be a drop, so there are
  fewer of them, and a fall in checkout, login or payment is close to always
  worth more than a rise in an impression counter.
- **Scopes with no baseline get their own group at the end.** A counter that
  went from nothing to something is usually a new event shipping, not an
  incident, and ranking by percentage would otherwise pin it to the top —
  an undefined ratio has no magnitude to sort by.
- **The first line is a summary you can trust**, e.g. `24 alerts · 7 down,
  17 up · worst checkout:complete:annual down 86%`. It is computed, never
  written by the AI, so it is correct on the morning the model is off or slow —
  and it is what your phone shows in the notification preview.
- The window the digest covers is stated in the project's timezone, because a
  digest is separated from its data by up to a whole day.
- **A digest that needs more than one message is still one digest.** Every part
  repeats the same summary and the same window — they describe the digest, not
  the part — and the window line carries a `2/3` marker so the alert count and
  the number of lines under it are not read as items lost.

The AI note, when the rule has one enabled, is written over **all** the
window's events at once and sits above the list.

:::tip
**Links need a format that has them.** On `plain` there is no link syntax, so
the digest keeps the full URL on its own line and a 24-alert morning is two
messages. Switch the rule's message format to **Telegram HTML** (or Slack
mrkdwn) and the same digest is one message at about a quarter of Telegram's
size limit — the URL moves behind the event name and stops counting against it.
:::

This layout is what every channel receives. A Slack or email digest is laid out
exactly like a Telegram one — same grouping, same summary line, same AI note
over the whole window.

**One message, not one per monitor.** When several rules on a Slack or email
destination match inside the same window, the digest goes out as a *single*
message carrying each rule's section, rather than one message per rule. The
Delivery log still records one row per rule — that is what keeps each rule's
own template, its Inbox incidents and its Retry working — so a digest of three
monitors is three rows and one message.

Telegram, webhook, Jira and Linear still send one message (or ticket) per rule.
Telegram already splits a single rule across several messages to fit its
4096-character ceiling and resumes a partial send per rule, and a Jira or Linear
ticket is per rule by contract; bundling either would cost more than it buys.
Their alerts are still held and released on the schedule — only the packing
differs.

:::note
**On a cadence, the cadence is the rate limit.** A rule's
[cooldown](#rules--what-fires-an-alert) is not applied a second time on top of
it: with the default 1440-minute cooldown and a daily digest, two limiters of
the same period would leave every other digest empty. A scope still has to
produce a *new* reading to be re-reported, so a digest never repeats a figure
nothing has updated.
:::

Changing the cadence starts the clock fresh — switching to "daily at 09:00" in
the afternoon delivers tomorrow at 09:00, and never dumps a backlog the moment
you save. What was already held is carried into the new schedule rather than
lost, and goes out in its first window.

**Switching back to "Immediately" is the one case that does not carry it over.**
The hold is over, so what was being held is discarded, and those scopes are
reported again from the next collection instead — with their *current* numbers,
which is what "Immediately" means. Nothing arrives the instant you save, and
nothing arrives twice. A scope that fell quiet while it was held is simply not
reported at all: the only thing lost is a measurement nobody can act on, and a
scope that is still firing is back within one collection. Anything the last
digest already reported stays quiet until its cooldown lapses, so saving the
change is not a re-announcement of everything the destination knows about.

Disabling a destination also starts the clock fresh and discards what it was
holding, the same way it already clears the rest of its alerting state.

Muting or acknowledging an incident during the window still works: the
[Inbox](#the-inbox) keeps tracking it while it waits, and a monitor you mute
before the digest goes out is left out of it.

### Testing a destination

**Test** on a destination card sends one fixed, clearly-marked message through
the channel itself —
`POST /api/v1/projects/{slug}/alert-destinations/{destination_id}/test`, editor
or owner only. It is the difference between "a bot token is stored" and "a bot
token works": a revoked Telegram token, a webhook whose channel was archived, and
a perfectly healthy destination all look identical in the form.

The reply is `{ "ok": …, "error": …, "sent_at": … }`, and:

- **It always answers 200.** A channel refusing the message is the answer you
  asked for, not a fault on our side, so a refusal comes back as `ok: false` with
  the channel's own message rather than as a 5xx the UI would render as "tripl is
  broken". `error` is `null` on success and `sent_at` is `null` on failure —
  both keys are always present.
- **It works on a disabled destination.** Disabled means "route no alerts here";
  checking credentials before switching one back on is the commonest reason to
  press Test, so refusing would make the button useless exactly when it is
  wanted.
- **It records no delivery.** A test is not an alert — writing one would mean
  borrowing a real rule and scan and claiming they fired, and it would stamp that
  rule's cooldown and silence the next genuine alert. What is recorded is the
  operator action, in the project **audit log**, naming the destination it was
  pressed on. The **Delivery log** tab below therefore keeps meaning "an alert
  fired".
- **A demo project refuses it**, with `ok: false` and an explanation: a demo is
  zero-egress. The exception is the local demo sink, which answers `ok: true`,
  because rendering and recording locally is exactly what a real delivery through
  it does. Test sends use the same demo egress guard as queued deliveries.
- **A failure says what kind it is.** Beside the channel's own `error` text the
  response carries `error_kind` — `http_status` (with `http_status`, the code),
  `dns`, `timeout`, `tls`, `network`, `smtp`, `config` (a stored value our own
  validators refused), `policy` (the demo refusal) or `other` — so the card
  names the cause in plain language and keeps the raw text under **Details**.
- **The result stays on the card until the channel's settings change.** Changing
  a stored setting or opening the destination's editor clears it, because the
  editor can replace a secret. Writes that leave the channel as it was keep it:
  toggling **Enabled**, or a digest being sent. **Dismiss** clears it by hand.

#### Send test — before the destination is saved

The destination dialog has its own **Send test**, so a channel can be checked
while it is being set up or edited, not only after **Create**:
`POST /api/v1/projects/{slug}/alert-destinations/test`, editor or owner only.
The result shows inside the dialog, and it reads the same way as a card's.

- **The body is the create payload**, with an optional `destination_id` when the
  dialog is editing a saved destination. A secret left blank in the form (a bot
  token, a webhook URL, an API key) is then taken from that destination's stored
  one, so a stored secret can be tested without typing it again. A stored
  secret only goes back to where it was saved for: the stored Jira API token is
  lent only when the draft's Jira base URL has the same scheme and host (and
  port) as the saved one, and a stored webhook header value only when the
  draft's target URL does. Otherwise the test returns `ok: false` with
  `error_kind: config`, and the secret has to be typed again to test the new
  host.
- **The channel cannot change.** A `destination_id` whose channel is not the
  body's `type` is a **422**; another project's destination id is a **404**,
  never a way to borrow its secrets.
- **The same refusals apply.** A demo project refuses it with the same
  `ok: false` and `error_kind: policy` (the local demo sink excepted), and the
  webhook and Jira URLs go through the same private-host refusal as a real
  delivery.
- **Nothing is saved.** No destination is created or changed, and no delivery is
  written. The press is audited as `alert_destination.test`, the same action as
  the card's **Test**; its payload has `draft: true` and `target_origin`, the
  scheme and host of the webhook or Jira URL that was tested.

Whoever reads that channel did not ask for the message, so it says on its own
line that nothing is wrong and that someone pressed Test. Use rule replay to
validate *matching*, and confirm the first real delivery in the **Delivery
log**; a failing webhook or an unverified bot token is the most common transport
failure.

### What deleting one would destroy

Deleting a **rule** deletes its deliveries with it, and deleting a **destination**
deletes every rule under it and every delivery under those. The Inbox reads
through those same deliveries, so the incidents they carried go too. That makes
"Delete?" the wrong question to ask, and both cards state the damage instead —
the numbers come back on the destination and rule payloads themselves:

| Field | On | Means |
|---|---|---|
| `total_deliveries` | a rule | Every delivery this rule has ever made |
| `incident_count` | a rule | Distinct incidents those deliveries carried |
| `delivery_count` | a destination | Every delivery through this destination |
| `incident_count` | a destination | Distinct incidents across all of its rules |

A destination's `incident_count` is **not** the sum of its rules'. Two rules of
one destination can carry the same incident, and adding two distinct counts would
report that incident twice, so the destination total is counted in its own right.

:::note
`total_deliveries` on a rule is the same all-time number
`GET /monitors/{rule_id}` reports under that name — a monitor *is* an alert rule,
so it is one number with one name. Do not confuse it with `delivery_count` on an
**Inbox incident**, which counts the deliveries of that one incident.
:::

### What a rule reports about its own state

Alongside those counts, a rule carries its mute state and its delivery health, so
its card can answer "is this silenced, and has this channel ever actually carried
anything" without a second request:

| Field | Means |
|---|---|
| `muted` | The rule is muted **right now**. A `muted_until` that has already passed is *not* muted. |
| `muted_until` | The instant the mute lifts — emitted **raw**, whether or not it has passed. |
| `last_delivery_at` | When this rule last sent anything, or `null` if it never has. |
| `last_delivery_status` | That same delivery's `pending` / `sent` / `failed`; `null` whenever `last_delivery_at` is. |

All four are the values `GET /monitors/{rule_id}` already reports for the same
rule, under the same names — a monitor is an alert rule seen from the other side,
and one object may not carry two shapes.

The monitor detail page also says **which** scopes are firing, not only how
many. Under the stat strip a **Firing now** panel lists each firing scope: its
direction (spike or drop arrow), its kind and name, and when its last anomalous
bucket started, with the row linking to that scope's monitoring drilldown. A
scope the rule has not notified about yet (a cooldown or a mute held the first
message back) has no delivery to take a name from, so it reads **not notified
yet**. The panel is absent while nothing fires. The API carries the same list as
`firing_scopes` on `GET /monitors/{rule_id}`.

:::note `muted_until` is not the question to ask
Read **`muted`**. `muted_until` on a rule is the stored timestamp and keeps being
sent after it lapses, so "muted until \<a past date\>" is a normal thing to see on
an unmuted rule; it means "when the last mute was set to lift", not "this rule is
silenced". An **Inbox incident** answers this differently — see
[What an incident row carries](#what-an-incident-row-carries).
:::

### What a Webhook destination POSTs

The body is JSON, so downstream automation (Zapier, n8n, your own service) can
read individual fields instead of scraping the rendered `message`:

```json
{
  "project":     { "name": "Checkout", "slug": "checkout" },
  "destination": { "id": "…", "name": "Ops Webhook" },
  "rule":        { "id": "…", "name": "Volume drops" },
  "scan":        { "id": "…", "name": "Hourly scan" },
  "matched_count": 1,
  "message": "…the same text the chat channels would receive…",
  "items": [
    {
      "scope_type": "event",
      "scope_ref": "…",
      "scope_name": "purchase:success",
      "direction": "drop",
      "actual_count": 10,
      "expected_count": 20,
      "absolute_delta": 10,
      "percent_delta": 50.0,
      "bucket": "2026-04-11T09:00:00+00:00",
      "details_url": "…",
      "monitoring_url": "…",
      "drift_field": null,
      "drift_type": null,
      "sample_value": null
    }
  ]
}
```

:::warning `percent_delta` is `null` when there is no baseline
`"percent_delta"` is **`null`**, not `0`, whenever `"expected_count"` is
**exactly `0`** — a scope resuming after an outage, an event firing for the
first time, a schema drift. `null` means *there was no baseline to divide by*,
so the ratio is undefined; it never means "no change". Reporting `0` there would
tell a consumer testing `percent_delta > threshold` that nothing changed about
the anomalies that changed the most. Use `"absolute_delta"` for that class; it
is the number that means something.

**Zero is the whole of the condition.** A **negative** expected count is a real
baseline — catalog metrics may be signed, and `-100` expects `-100` — so an
anomaly against one carries a real `percent_delta`, not `null`. It is computed
from the **magnitude** of the expectation, `|actual − expected| / |expected| ×
100`, which is the same divisor `min_percent_delta` is scored against, so the
number a rule fired on and the number the payload reports are the same number.
That makes `percent_delta` a **size and never a direction**: an actual of `-300`
against an expected of `-100` reports `200.0`, not `-200.0`. Read `"direction"`
(and `"actual_count"` against `"expected_count"`) for which way it moved.

The same rule applies to the item list inside a delivery's `payload_snapshot`
and to the typed `items[]` array of
`GET /projects/{slug}/alert-deliveries/{id}` — one delivery cannot answer the
same question two ways.

Deliveries recorded **before this behaviour shipped** still carry `0.0` in their
stored `payload_snapshot` — a delivery is a frozen record and is not rewritten.
Read `expected_count == 0` to disambiguate historical zero-baseline rows. A
delivery recorded against a **negative** baseline before the magnitude rule
landed is the one case reading `expected_count` cannot rescue: its stored `0.0`
is a placeholder, but the expectation is non-zero, so it now renders as a
genuine-looking `0.0` rather than as `null`. Only deliveries from that window are
affected, and only on signed metrics.
:::

#### The test POST is a different body

Pressing **Test** on a webhook destination (see
[Testing a destination](#testing-a-destination)) POSTs to the same URL with the
same optional header, but the body is **not** the one above:

```json
{
  "event": "tripl.destination_test",
  "destination": "Ops Webhook",
  "message": "…Someone pressed Test in Tripl to check that this channel is reachable. No alert fired and nothing is wrong."
}
```

Switch on the `event` key to tell them apart: an **alert** body has no `event`
key at all, and a test body always carries `"tripl.destination_test"`. That is
why the marker is a typed field rather than only a sentence in `message` — a
receiver that opens a ticket per alert must be able to drop a test without
parsing prose. A test body carries no `items`, no `rule` and no `scan`, because
no rule fired and no scan produced it.

:::note
The test POST is subject to the same SSRF guard as a real send: the target is
re-resolved and refused if it points at a private or link-local address.
:::

### The AI note remembers what it already told you

When **AI explanation** is on for a rule, the note is written with the last
week's sent alerts for the *same scopes* in front of it — up to three, and only
ones that actually went out. So a second alert about the same event opens with
what changed ("still falling, now 90% below expected") instead of repeating the
first note word for word. A genuinely first-time alert has no history to carry
and reads exactly as before.

Matching is by scope, not by rule: a rule watching a hundred events will not
recall an unrelated event's history as if it were this one's. Failed deliveries
are never recalled — nobody read them.

:::note
**Email is all-or-nothing.** If the SMTP server refuses *some* recipients, the
whole delivery is recorded as **failed** and the error names the addresses that
bounced. Retrying re-sends to everyone on the list, including anyone who already
received it — a duplicate is preferable to believing an alert was delivered when
it was not.
:::

Enabled **Slack** and **Email** destinations on a real project also receive two
scheduled messages that no rule controls: the **weekly plan digest**, and a
**daily notice naming deprecated events that are still receiving data** past the
sunset date the plan gave them. A demo project's destinations receive neither,
because a demo project is zero-egress — the worker leaves it out for the same
reason the API refuses it a real destination.

The weekly digest counts metric anomalies by their **observation bucket** in
the reporting week, the same window definition used by rule replay. A backfill
written this week for an older bucket does not raise this week's count. This
changes the former digest behavior, which counted metric anomalies by when
their rows were written (`created_at`); totals around backfills may therefore
differ from earlier weekly digests.

The sunset notice is the digest's "deprecated events still receiving data"
count expanded into named events, each with its sunset date and the day it was
last seen. Both read the **main** plan branch only, so the count and the list
always agree, and an open working branch never makes an event appear twice. The
notice keeps no memory of what it has already said: the same list arrives every
day until someone retires the event or stops the data reaching it, which is the
point — data still flowing into an event the plan retired is a standing
condition, not a moment. A project with nothing overdue is sent nothing.

Both messages are destination-level and independent of routing rules; disable
the destination if it should receive neither alerts nor either of them.

## Rules — what fires an alert

A rule decides which signals reach its destination. The controls:

Rule names can be up to 255 characters long; the editor enforces the same limit
as the API and database.

**Scan — which scan's signals to act on.** Defaults to **All scans**: the rule
reacts to every scan in the project. Pick a single scan to narrow it — see
[Narrowing a rule to one scan](#narrowing-a-rule-to-one-scan) below.

**Scope — which kinds of signal to act on.** Volume anomalies are on by default;
the drift/regression signals are opt-in:

| Signal | Default |
|--------|---------|
| Project-total volume | on |
| Event-type volume | on |
| Event volume | on |
| Schema drift | off |
| Distribution drift | off |
| Variable value drift | off |
| Release regression | off |
| Metric anomaly | off |
| Source freshness | off |
| Lifecycle | off |

The two drift scopes act on signals something else in the project has to produce
first, so one of them can be switched on and still be unable to fire — see
[When a scope is on but nothing feeds it](#when-a-scope-is-on-but-nothing-feeds-it).

**Metric anomalies** are opt-in via a rule's **`include_metrics`** field — the
**Metrics** box in the rule editor, off by default. Unlike
the drift and regression signals they behave like a volume anomaly — they carry
a real spike/drop direction and **do** honor the count thresholds below.

**Direction.** *Notify on spike* and *notify on drop* (at least one must be on).
Schema, distribution, and variable-value drift are reported as a **spike**;
release regressions and source freshness are reported as a **drop** — so a
drift-only rule still needs *notify on spike* enabled, and a rule that should
hear about late data needs *notify on drop*. **Lifecycle** alerts are the
exception to both switches: a rule with **Lifecycle** on receives every
lifecycle finding whichever directions it notifies on, because an overdue sunset
or a silent successor is not a move against a baseline — see
[Lifecycle](#lifecycle).

**Thresholds** — gate the noise on **volume anomalies only**:

- `min expected count` — ignore low-traffic buckets,
- `min absolute delta` — require at least N events of change,
- `min percent delta` — require at least N % of change. A new rule starts at
  **`30`**, in the rule editor and through the API alike; rules saved before
  that keep the value they were saved with. The percentage is measured against
  the expectation, so it is not symmetric: a spike reaches 100 % at double the
  expectation, a drop only at zero. A scope going dark is
  exactly 100 % and still alerts, and so does one that starts firing where
  nothing was expected: a percentage has nothing to divide by at a zero
  baseline, so the percent gate steps aside there and `min absolute delta`
  decides. (No movement against a baseline of zero is still not an event.)
  Those alerts say **`no baseline`** where the others carry a percentage —
  in the message, in the delivery's item table, in the simulator, in the AI
  explanation, and in the monitoring surfaces that work a percentage out for
  themselves (the signal banner on a scope's page, the **Top movers** rows) —
  because there is no ratio to report. The absolute delta beside it is the
  number that means something. Programs get the same fact as JSON `null` rather
  than the words; see
  [What a Webhook destination POSTs](#what-a-webhook-destination-posts).

:::tip Why the percent default is 30, not 0 or 100
The percent gate is asymmetric: a spike reaches `100` at double the expected
volume, but a drop reaches `100` only when volume falls to zero. A rule at
`100` therefore hears about a scope going dark and ignores a 50 % or 90 % fall,
which is why new rules start at `30` and the rule editor warns when drops are
on at `100` or more.

Lower is not free. Most volume anomalies are single-bucket seasonal deviations
rather than sustained shifts, and a busy catalog oscillates in both directions
within the same day. On a real 2,500-event iOS catalog, replaying 24 hours of
collections produced 436 matches at `0`, 267 at `50`, and 37 at `100`. If `30`
is too loud for a noisy catalog, raise it, keep drops on, and use the
simulator to see what the new value would have sent.
:::

:::warning
Thresholds apply to the volume scopes (project total / event type / event) and to
**metric anomalies**. Schema drift, distribution drift, variable-value drift,
release regressions and lifecycle findings **bypass** thresholds — if you enable
those scopes, they fire regardless of the count thresholds.
:::

### Source freshness {#source-freshness}

When a warehouse load is delayed, every scope on a scan looks low at once. The
**Source freshness** scope reports that delay once, as a problem with the
source, rather than as a drop on every scope. It is opt-in through the rule's
**`include_source_freshness`** field, which is off by default. The
[demo workspace](./demo-workspace.md) seeds a rule with it switched on.

Each scan's freshness is `fresh`, `late`, `overdue` or `unknown`. Roughly,
`late` means the newest event is too old, and `overdue` means the scan itself
has not completed a collection on schedule. The exact bounds are in
[Drop signals are held while a source is late](./anomaly-detection.md#held-while-late).

- **One candidate per scan.** Every scan that is `late` or `overdue` produces
  one candidate with scope type `source_freshness` and direction `drop`. A scan
  that is `fresh` or `unknown` produces none. A rule
  [bound to one scan](#narrowing-a-rule-to-one-scan) only sees that scan's
  candidate.
- **Where the candidates come from.** A `late` candidate comes from the scan's
  own collection run. An `overdue` candidate comes from a sweep that runs every
  15 minutes, since an overdue scan is not running to raise it.
- **One delay, one alert.** The candidate goes through the same correlation and
  cooldown as every other alert. A delay or outage that lasts several scans is
  therefore delivered once, not once per scan run.
- **The rule needs drops.** The candidate's direction is `drop`, so the rule
  must have *notify on drop* on.
- **The volume drops stay quiet.** While the scan is late or overdue, the
  detector holds its drop-direction volume anomalies (project total, event
  types, events and breakdowns). Those drops do not reach this rule or any other
  rule. After the data lands, the next run re-collects the buckets that were
  empty during the delay and scores them normally.

The message names the scan, the age of its newest event, and the window it was
expected within:

```
Data late: Production events — newest event 7h ago (expected within 3h)
```

### Lifecycle {#lifecycle}

A retirement that is not happening is easy to miss: nothing spikes or drops, an
old event just keeps firing past its sunset date, or its replacement never
starts. The **Lifecycle** scope turns the daily
[sunset watch](./feature-reference.md#sunset-watch) into alerts. It is opt-in
through the rule's **`include_lifecycle`** field — the **Lifecycle** box in the
rule editor — which is off by default.

- **One candidate per open finding.** Every open lifecycle finding in the
  project produces one candidate with scope type `lifecycle`, naming the
  deprecated event and the finding's kind:
  - `sunset_overdue` — a deprecated event past its sunset date that still
    received events in the last 24 hours, with that 24-hour count;
  - `successor_silent` — the event named as a deprecated event's replacement
    received nothing in the last 7 days. The alert is still about the
    deprecated event; the message names the quiet successor alongside it.
- **Project-wide, not per scan.** A finding is about an event, not a scan, so
  every scan run in the project offers the open findings, and they are
  deduplicated project-wide: a project with several scans still gets one alert
  per finding, and a rule [bound to one scan](#narrowing-a-rule-to-one-scan)
  that has **Lifecycle** on receives them whichever scan it is bound to.
- **One alert per finding episode.** An alert is keyed on the moment the finding
  opened. While the finding stays open, each scan run offers it again and it is
  recognised as the same alert, not a new one. A finding that resolves and later
  opens again is a new episode and alerts again. A resolved finding produces no
  candidate.
- **No direction or threshold gates.** Lifecycle alerts ignore *notify on
  spike* / *notify on drop* and the count thresholds: an old event still firing
  and a replacement never firing are not spikes or drops against a baseline, so
  a rule that switched **Lifecycle** on receives all of them. Its filters still
  apply.
- **Digests carry them like any other item.** On a destination with a
  [delivery schedule](#delivery-schedule), lifecycle candidates are collected
  into the digest with everything else the rules matched.

Lifecycle findings are computed on `main` only, so a retirement documented on a
working branch is not watched until the branch merges.

### When a scope is on but nothing feeds it

Enabling a scope narrows what a rule reacts to; it never creates the signals.
The two drift scopes depend on plan and scan configuration a rule does not own,
so a rule can have one of them switched on and still be structurally unable to
fire — no error anywhere, just permanent silence.

**Variable value drift** needs some variable to document an allowed-values list
on the **main** branch, *or* a value drift already collected in this project.
Either documented source counts: the variable's own list of allowed values, or a
per-event override of it. One of them is enough. Values documented on a working
branch change nothing until that branch merges, because detection runs against
main, and a variable excluded from scans never drifts however full its list is.
Collected drift counts on its own for the same reason it does for distribution
drift — candidates are built from the drift rows, so an open or snoozed row from
the last 30 days keeps the scope live even after the documented list that
produced it is emptied. The exclusion rule reaches those rows too: excluding a
variable from scans keeps the drift it already had, but alerts skip that drift,
so it no longer counts towards readiness either. A project whose only surviving
value drift sits on excluded variables reads as a scope that cannot fire.

**Distribution drift** needs a scan that names the columns to watch (**Scan
settings → Metric breakdowns and drift → Distribution drift**), *or* a
**significant** drift already collected in this project. Either one is enough —
candidates are built from the drift rows, and only a significant one ever
becomes a candidate, so a project that has collected a significant drift keeps
the scope live even if the scan's field list is later emptied. A history of
stable or minor scores does not count on its own: nothing can turn those rows
into an alert.

When neither source exists, the rule editor and the monitor detail say so
inline, beside the box you just ticked:

- *Value drift is on, but no variable that scans observe documents an
  allowed-values list on the main branch — this scope cannot fire until one
  does.* The notice links to **Variables**, and adds that Variables opens on the
  branch you have selected — a list documented on a working branch counts only
  once it merges. (A variable excluded from scans does not count, which is what
  "that scans observe" means.)
- *Distribution drift is on, but no scan in this project watches a column for
  it — this scope cannot fire until one does.* The notice links to **Scan
  settings**. On the monitor detail, a rule bound to a single scan gets a link
  straight to **that scan's** settings; an **All scans** rule, and the rule
  editor in every case, links to the scan list.

On the monitor detail the notice sits under the scope chips in the **Condition**
panel, and the affected chip itself is flagged and repeats the sentence on hover.
The checkbox in the editor stays enabled: the precondition can be satisfied
later, and locking the toggle would report a problem from the one screen that
then refused to let you set the rule up before the data exists. In the editor
the notice's link opens in a **new tab**, so acting on it does not close the
dialog and discard a half-built rule; on the read-only monitor detail it opens
in the same tab.

Programs read the same fact from `scope_readiness` on `GET /monitors-summary`
and `GET /monitors/{rule_id}` — two booleans, `variable_value_drift` and
`distribution_drift`, with the same meaning on both responses. It answers *could
this scope ever produce a candidate in this project*, not *will this rule fire*.
Whether a rule fires also depends on thresholds, filters, mutes and a scan
actually running, and readiness says nothing about any of those.
`GET /monitors/{rule_id}` additionally carries `scan_config_id` and
`scan_name` — the scan the rule is narrowed to, both null on an **All scans**
rule. They name the binding; they do **not** narrow `scope_readiness`, which is
still the project-wide answer on both responses.

:::warning Readiness is project-wide, and not scan-aware
`scope_readiness` is one fact about the whole project. A rule
[bound to a single scan](#narrowing-a-rule-to-one-scan) therefore shows no
warning as long as *some* scan in the project watches a column for distribution
drift — even when the scan that rule is actually bound to watches none. For a
scan-bound rule, check that scan's own **Distribution drift** list before
concluding the scope can fire. The monitor detail now names that scan for you:
the **Condition** panel's **Scan** row shows which one to open, so the check is
no longer a hunt for *which* scan. The row is text, though — opening it is still
a trip through **Scans**. The direct link to a scan's own settings appears only
in the notice above, and that notice shows only when *nothing* in the project
feeds distribution drift, which is the opposite of the case this warning is
about. The verdict itself is still the project's.
:::

### Narrowing a rule to one scan

The **Scan** picker in the rule editor binds a rule to a single scan
configuration. **All scans** (the default, and what every rule created before
this option existed still has) keeps the original project-wide behaviour, so
nothing changes unless you pick a scan.

A rule's binding is also shown on its monitor detail: the **Condition** panel's
first row is **Scan**, reading **All scans** when the rule is project-wide. You
can tell a narrowed rule from a project-wide one without opening the editor.

Use it when one scan is materially noisier or less valuable than the rest — a
legacy or archived-data scan, for example — and you want it out of a channel
without weakening the thresholds that the other scans depend on. Filters cannot
do this: they only understand `event_type`, `event`, `metric` and `direction`, so there is
no filter expression that names a scan.

A common shape is two rules on the same destination: one bound to the important
scan with sensitive thresholds, and one on **All scans** for the drift signals
you always want.

:::note Metric anomalies do not honour a scan binding
Catalog **metric** anomalies are project-wide — a metric series is computed for
the project, not for one scan — so a rule bound to a scan has nothing to say
about them. On such a rule the **Metrics** scope is inert: metric anomalies are
delivered only by rules left on **All scans**.
:::

:::note What happens when the scan is deleted
Deleting a scan does **not** delete the rules bound to it, and does not silently
re-aim them at the whole project (which would start paging on every other scan).
Each such rule is unbound back to **All scans** *and disabled*, so it keeps its
name, thresholds, templates and filters and is visible, switched off, on the
Alerting tab until you re-aim and re-enable it.
:::

**Filters** narrow further by `event_type`, `event`, `metric`, or `direction`,
with operators `eq` / `ne` / `in` / `not_in`. Multiple filters are ANDed; a
signal that doesn't carry the filtered field passes through. A `metric` filter
names catalog metrics by id and narrows only **catalog metric anomalies** — every
volume and drift signal passes it through — so a rule meant for one metric
alone should also switch the other scopes off.
A filter row with no value picked is refused when you save the rule. Pick at
least one value or remove the row. An empty row is never dropped quietly,
because the rule would then save broader than the form showed.

An `event_type` filter narrows **any** signal that carries an event type. For
most of them the type is stored on the signal's own row; for the ones anchored to
an event — event scope, variable-value drift and event-scope release regression —
it is looked up when the rule is matched, because those rows deliberately keep no
type of their own. An `event` filter is narrower still: it reaches only the
signals that name one event. **Passes through** below means the filter has
nothing to say about that signal, so the signal is still delivered.

| Signal | `event_type` filter | `event` filter | `metric` filter |
| --- | --- | --- | --- |
| Event-scope anomaly | Narrows, by the event's type | Narrows | Passes through |
| Variable-value drift | Narrows, by the event's type | Narrows | Passes through |
| Release regression found on an **event** | Narrows, by the event's type | Narrows | Passes through |
| Event-type rollup | Narrows | Passes through | Passes through |
| Schema drift | Narrows | Passes through | Passes through |
| Distribution drift on one event type | Narrows | Passes through | Passes through |
| Release regression found on an **event type** | Narrows | Passes through | Passes through |
| Distribution drift across the whole scan | Passes through | Passes through | Passes through |
| Project-total rollup | Passes through | Passes through | Passes through |
| Catalog metric anomaly | Passes through | Passes through | Narrows |

The scan-wide distribution drift row is the one that catches people out. A scan
watching a column for distribution drift always produces a row for that column
across the whole scan — the one an alert names **All events.`column`** — as well
as one row per event type where the scan can tell which type each warehouse row
belongs to. The scan-wide row is about every event at once, so it has no type to
be narrowed by, and no `event_type` filter can exclude it: a rule filtered
`event_type not_in ['Screen View']` still delivers it. Silence it at the source
instead, by taking that column off the scan's **Distribution drift** list, or
take distribution drift off the rule's scopes.

The `event` value picker searches the catalog server-side and shows one page of
matches at a time, so type to reach an event that isn't in the first page — the
footer tells you how many matches are still hidden.

Variable-value drift carries its affected `event_id`, so event filters apply;
its alert item uses the variable name as `drift_field` and a bounded novel-value
sample as `sample_value`. That same `event_id` is what `details:` links to: the
event's monitoring page carries the **Value drift** panel, which lists the full
set of observed values the message could only sample, and lets you accept,
snooze or dismiss the drift from there.

**Cooldown** suppresses repeats. The editor takes it as an amount and a unit
(minutes, hours or days) and stores minutes. Default **1440 minutes (24h)**, tracked
separately per *(rule, scan, scope)* and measured from the last message that was
actually delivered. A **catalog metric** is the exception, because it is not a
scan's series in the first place: it is measured once for the whole project, so
it gets **one clock per *(rule, scope)*** that every scan shares. Tell someone
about it once and it stays quiet for the cooldown however many scans the project
runs, rather than once per scan. It applies to destinations that deliver
**immediately**; on
a destination with a [delivery schedule](#delivery-schedule) the cadence is the
rate limit instead. A rule fires when the anomaly first opens, when it re-opens
after recovering, or when a newer anomaly bucket appears — in every case only
once the cooldown has elapsed. A scope that recovers and relapses within the
cooldown is still recorded as firing; you just aren't told twice.

:::tip
Before saving, use the **simulator** to replay a rule over the last *N* days and
see how often it would have fired — it flags a rule as noisy past ~50 firings, so
you can tighten thresholds or cooldown first. Replay keeps the result contained
and its Close action visible; on a narrow screen, scroll the firing table itself
to inspect all columns without losing the rest of the dialog.
:::

### Replaying a what-if without saving it

Opening **Replay** from a rule's **…** menu runs it straight away over the
default window; change the window or an override and run it again.

Replay answers "how noisy is this rule", and it also answers "how noisy would a
*different* rule be" — without editing a rule that is live-routing to a real
channel while you find out.
`POST /api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate`
takes `days` plus four optional overrides, each applied for that one run only and
written back nowhere:

| Override | Replaces | Notes |
|---|---|---|
| `cooldown_minutes_override` | `cooldown_minutes` | `0` disables grouping, so every match becomes a firing |
| `min_percent_delta_override` | `min percent delta` | The threshold this exists for: "would 300 cut these?" |
| `min_expected_count_override` | `min expected count` | Ignore low-traffic buckets, as the saved value does |
| `sigma_threshold_override` | the **detector's** sensitivity | Not a rule setting — see below. Must be **greater than 0 and at most 10**, the same ceiling the false-positive ratchet respects; outside that the request is a 422 |

Each comes back as a `*_used` / `*_saved` pair (`min_percent_delta_used`,
`min_percent_delta_saved`, and so on), so the result can show *tried* beside
*stored* without a second request. Omit an override and `used` equals `saved`.
In the replay dialog a blank box means "use the saved value". A box holding
something outside the bounds, such as a negative number, is marked invalid and
blocks **Replay**. It is not read as blank.

The request body can also carry a **draft**: the same body a rule edit saves
(`PATCH …/rules/{rule_id}`). The replay then runs the saved rule with the
draft's changes laid over it — checked the way Save checks them, and written
nowhere. The overrides above apply on top of the draft, and the `*_saved`
fields still report the rule as stored. In the rule editor, **Replay saved
rule** replays the rule on file; once you have changed something on the form,
**Replay with these edits** replays the form as it stands, before you save it.

Each firing also reports its scan. The preview table shows that scan's name,
or **Project-wide** when the anomaly has no scan, so similarly named scopes
from different scans remain distinguishable.

**Every scope a rule can fire on is replayed**, the opt-in ones included: volume
anomalies, catalog metrics, schema drift, distribution drift, **variable-value
drift** and **release regressions**. If you had switched those last two on and a
replay kept coming back empty for them, that was the replay and not your rule —
those two scopes were not being read at all, so a rule that pages on them daily
replayed as perfectly quiet. They now count everywhere the other scopes do: in
the anomalies considered, in the firings, in the noisy verdict, and in the
previewed message.

**A release regression makes the count a floor rather than an estimate.** Tripl
keeps one regression per scope and release — the current verdict, not a record of
every collection that saw it — so a replay can only place a standing regression
once, at the window it was measured over. Live, the same regression is re-sent
once per cooldown for as long as the release stays behind. Read a
release-regression row in the firing table as *at least once*: it is the one
scope where replay under-counts instead of predicting, and a rule that looks
borderline on regressions alone will be louder than the number says.

`sigma_threshold_override` is the odd one out, because sigma is not a rule
control at all: it is the **detector's** sensitivity — one value for the whole
project, under **Settings → Monitoring → Detection settings** — and it decides
whether an anomaly was **recorded**. Replay reads anomalies that already exist,
so a **higher** value re-reads them and drops the ones whose `|z|` no longer
clears the bar — those disappear from `anomalies_considered` too, not just from
the firings, because in the world you are asking about they were never written. A
**lower** value cannot bring anything back: rows below the project's threshold
were never stored. Drift and release-regression signals carry no z-score and are
untouched by it, exactly as they bypass the rule thresholds.
`sigma_threshold_saved` is that Detection-settings value — the same number for
every rule in the project, whether the rule is bound to one scan or left on **All
scans** — and a project that has never opened that screen is quoted the **4.0**
it would start with. A scope that a **false positive** has tightened is detected
against a stricter threshold than this one; the replay quotes the project-wide
base, which is the only figure a single field can honestly carry.

### Example

A rule that pages Slack only on meaningful drops in checkout volume:

- **Destination:** your Slack channel
- **Scope:** event volume (drift/regression off)
- **Direction:** notify on drop
- **Thresholds:** min expected count `100`, min percent delta `30`
- **Filter:** `event` `in` `checkout:completed`
- **Cooldown:** `360` (re-alert at most every 6 hours)

## Message templates

Messages are rendered from templates using `${variable}` placeholders (an unknown
variable is rejected, so a typo fails fast rather than sending a broken message).

- **Message-level:** `${project_name}`, `${project_slug}`, `${channel}`,
  `${destination_name}`, `${rule_name}`, `${scan_name}`, `${matched_count}`,
  `${items_count}`, `${items_text}`.
- **Per matched item:** `${scope_name}`, `${scope_type}`, `${scope_label}`,
  `${direction}`, `${direction_label}`, `${actual_count}`, `${expected_count}`,
  `${expected_basis}`,
  `${absolute_delta}`, `${percent_delta}`, `${percent_delta_label}`,
  `${bucket}`, `${details_url}`,
  `${monitoring_url}`, `${drift_field}`, `${drift_type}`, `${sample_value}`,
  `${sparkline}`, `${top_movers}`, `${attribution}`, plus pre-formatted
  `*_line` variants (`${details_line}`, `${monitoring_line}`, `${drift_line}`,
  `${attribution_line}`, `${sparkline_line}`, `${top_movers_line}`).
  `${attribution}` is the bare [attribution](#attribution-line) sentence;
  `${attribution_line}` is the same text on its own line, prefixed `why: `.
  Both are empty when the item has no attribution.

  `${percent_delta_label}` is the one the default templates use: it carries its
  own `%` sign and says `no baseline` when the expected count was zero, where a
  bare `${percent_delta}%` would print the undefined ratio as `0.0%`. Use
  `${percent_delta}` only if you want the raw number.

  **If you already saved a custom item template, check it for
  `${percent_delta}`.** A saved template is your text and nothing rewrites it, so
  a rule written before `${percent_delta_label}` existed goes on printing `0.0%`
  for every anomaly against a zero baseline — a scope resuming after an outage,
  an event firing for the first time, a schema drift — which reads as "nothing
  changed" about the anomalies that changed the most. The fix is one edit, in
  **Alerting → the rule → Message template**: replace `${percent_delta}%` with
  `${percent_delta_label}`, dropping the literal `%` you were writing after it
  because the label brings its own. Nothing else in your template moves, and
  rules still on the default template already say `no baseline`.

  `${expected_basis}` is empty for almost every item. It exists because one
  scope computes its expectation differently from all the others: a **release
  regression** compares shares, not counts, so its `${expected_count}` is
  followed by `(adoption-adjusted)`. If you write a custom item template and
  drop this variable, release-regression items lose that qualifier — see
  [Release-regression items](#release-regression-items) below for why it is
  there.
- **Email subject** supports a smaller set: `${project_name}`, `${project_slug}`,
  `${rule_name}`, `${destination_name}`, `${matched_count}`.

An optional **AI explanation** can be appended to messages; it is off by default
and does nothing unless an AI provider is configured — see
[AI & search providers](../run/ai-and-search.md).

### The attribution line {#attribution-line}

A volume anomaly item carries one extra line when tripl could say where the
change came from — the [attribution](./anomaly-detection.md#attribution)
stored with the anomaly, for example

> why: 92% of the drop comes from platform = ios (−3,120 of −3,390); Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop

The line is `why: ` followed by the attribution's **headline**, plus `; ` and the
**release line** when a new app version crossed the release gate shortly before
the bucket. Both sentences are built once, by the backend, from the attribution
stored at detection time, and are the very sentences the drilldown's **Why**
panel prints — the alert and the page it links to always read the same, down to
the digit. The headline takes one of two forms:

- *"92% of the drop comes from platform = ios (−3,120 of −3,390)"* — the
  breakdown column that explains the most of the change, its biggest value
  moving the same way, that value's share of the delta, and its contribution
  against the scope's whole delta;
- *"Platform shifted in both directions; no single value explains the drop"* —
  with no percent, when that column's values moved against each other so much
  that naming one would mislead.

The line is rendered under the item in the default item templates (as
`${attribution_line}`; `${attribution}` holds the bare text for custom
templates) and is given to the AI explanation, so the note reasons from the
same breakdown instead of guessing one. The one-line-per-item digest templates
leave it out, as they leave out the movers and sparkline lines. **Webhook
destinations do not carry it**: the delivery's `items[]` has no attribution
field — fetch it from
[`GET /anomalies/{anomaly_id}/attribution`](../integrate/agent-api-guide.md#signal-attribution)
if an integration needs it. The rule editor's preview leaves it out too, since a
simulated firing has no stored anomaly behind it.

The line is left out, rather than printed empty, when the item has no
attribution: a scan with no breakdown column, an anomaly detected before
attribution existed, and every item that is not a volume anomaly on the project
total, an event type or an event (drift, release regression, catalog metrics).

## The page

**Observe → Alerting** is four tabs, selected with `?section=`:

| Tab | For |
|---|---|
| **Inbox** | Triage: incidents, their actions, and what was sent for each. The default, and where an alert link lands. |
| **Rules** | Every alert rule in the project with its live firing state; replay, mute, edit and delete sit behind each row's **…** menu. |
| **Destinations** | The channels rules route to: configuration, a test send, and how much traffic each has carried. |
| **Delivery log** | Every delivery in the project, filterable, for "did the message actually go out". |

The **Inbox** tab carries the number of open incidents and **Delivery log** the
number of failed deliveries, in red, whenever either is above zero. On
**Rules**, the **Firing**, **Warning** and **Healthy** tiles above the table
filter it to the rules in that state; pressing the lit tile again shows every
rule.

The **Rules** tab was called *Monitors* until the product settled on one name,
**alert rule**, for this object (its `?section=` key is still `monitors`). It was
also a separate nav item until it was merged in. It listed the same
`AlertRule` rows this page already owned — a rule was read there and edited here
— so the two surfaces drifted about mute state. `/p/<slug>/monitors` now
redirects to `?section=monitors`; `/p/<slug>/monitors/<rule_id>` still opens that
rule's fired history.

The third tab is named **Delivery log** rather than *Audit*, because **Govern →
Audit log** already means something else entirely — who changed what — while this
one is the messages behind the Inbox's incidents. Its `?section=` key is still
`audit`, so every alert link written so far keeps working.

The sidebar badge beside **Alerting** counts **open incidents**, not
destinations: `open_incident_count` in the `summary` of
`GET /api/v1/projects/{slug}` is how many Inbox rows have an effective status of
`open`, worked out with the Inbox's own 30-day window and its own status rules —
including the one where a mute that has run out counts as open again. It used to
badge the destination count, so it read "Alerting 1" beside a page listing 52
open incidents; a badge that disagrees with the page it labels is worse than no
badge.

The section is a query parameter rather than a path segment because the second
path segment already carries the delivery id an alert link points at — links
already sent keep working, and one carrying `?incident=` opens the Inbox with
that incident expanded.

**"Monitor" was another name for these same rules.** It is not a separate
object — it is an alert rule plus its live firing state. For a while the two had separate homes:
rules were listed and edited on the destination cards, while their state lived
on a **Monitors** page under its own nav item. The result was one object with two
names on two screens, and they drifted — a rule could read "muted" on one and
fully live on the other.

They are now one tab. The **Rules** tab carries the rule, its state, and every
control that acts on it; **Destinations** carries only the channels, plus a rule
count so "wired up and nothing routes here" is still visible.

## Deliveries and the Inbox

Each match creates a **delivery** that moves through `pending → sent` or
`pending → failed`. Sending is idempotent for ticket and multi-part channels
(created issue ids and delivered parts are recorded mid-flight, so a re-run
does not repeat them); a plain message channel can, in the rare case where the
receiver accepted a send whose response then timed out, deliver twice — the
trade the pipeline prefers over a silently lost alert. A background reaper
requeues deliveries that get stuck (roughly every 5 minutes, up to a few
attempts); a delivery has to have sat unsent for fifteen minutes before it
qualifies, which is exactly how long a send keeps its hold on the row — so the
requeued attempt takes the delivery over rather than running beside one that is
still going. The
same reaper also retries a delivery that **failed on a transient network
error** — destination unreachable, connection refused, a timeout — a few
times, minutes apart, within that same attempt budget (only failures from the
last six hours are picked up, so a stale backlog is not resurrected after
downtime or a deploy). While an attempt is queued the row shows **pending**
but keeps its last error; a failed attempt returns it to **failed** with the
fresh error, and a success flips it to **sent**. Ticket destinations (Jira,
Linear) and destinations you have disabled are never retried automatically —
creating a ticket twice cannot be undone by a retry, and a disabled toggle
means silence. Every other failure — bad credentials, a rejected payload — is
never retried automatically either: fix the cause and press **Retry** in the
UI, which also resets the attempt budget, so a delivery you retry by hand
starts with a fresh set of attempts.

**Retry** on a Jira or Linear delivery asks for confirmation first, because the
new attempt opens a new issue. A retry that was accepted says so ("Retry
queued"); the delivery then goes back to **pending** until the worker sends it.
The page keeps an eye on it for about two minutes and reports the outcome:
"Delivered — … accepted the retried alert.", or "Still failing: …" with the
destination's error.

If a digest worker is interrupted, the reaper requeues stranded Slack and email
members through the digest sender, grouped by their original flush. Other
channels retain their normal per-delivery sender.

**The toggle is read when the message goes out, not when the alert was
decided.** A delivery created while a destination was enabled and sent after you
switched it off is marked **failed**, naming the destination, rather than
delivered — nothing is routed to a channel you have turned off, and nothing is
quietly dropped either. Switch the destination back on and press **Retry** if you
still want it.

The rule switch and mute are also checked just before a queued message leaves.
Disabling or muting a rule stops its pending delivery and records the reason as
**failed**. A manual **Retry** on a disabled destination returns 409; enable
the destination first.

A Telegram delivery carrying more than **8 matched items** is split into several
deliveries, because Telegram rejects a message over 4,096 characters outright.
Nothing is dropped — every match still reaches you, across as many messages as it
takes. Other channels have no comparable limit and keep one delivery per rule.

That item count is only an estimate of the ceiling, so the finished message is
measured against it too — the header, the items and the AI note as you will
receive them, counted the way Telegram counts, where an emoji costs two. A
message that does not fit is sent as several, each carrying whole items and
headed by its own count, with the AI note on the first. So a long custom item
template or an unusually long AI note costs you extra messages, never a missing
alert.

The one thing that cannot be split is a single alert item longer than 4,096
characters on its own. Telegram refuses that message and the delivery is marked
**failed** in the Inbox, saying how many of its items had already gone out. That
is deliberate: a failure is visible and can be retried once you shorten the
rule's item template, whereas silently rebuilding the same rejected message
every collection is not.

Because those messages go out one at a time, a delivery can fail after some of
them have already arrived — Telegram rate-limits a busy group chat, or the
connection drops mid-way. Retrying such a delivery, from the Inbox or from the
reaper, sends only the items you have not received yet, so a retry never repeats
an alert that is already in the chat. If every item had in fact gone out and only
the recording of it failed, Retry sends nothing and simply marks the delivery
**sent**.

### Release-regression items

Release-regression items read differently from every other alert line, and the
difference is deliberate.

Their `expected` is **not a count of the same thing as `actual`**. It is the
previous release's *share* of that scope applied to the new release's *own*
volume over the rollout-overlap window — so the message writes it as
`expected=715.7 (adoption-adjusted)` and spells the arithmetic out underneath:

```
- Release regression spot:open:wind:: down, actual=345, expected=715.7 (adoption-adjusted), delta=370.7 (51.8%)
  release: dropped in 15.7.5 vs 15.7.4 over the 51h rollout overlap; 715.7 is 15.7.4's share of this event at 15.7.5's own volume, so 51.8% is share-for-share
  details: https://your-tripl/p/windy-ios/alerting/<delivery-id>?item=release_regression:<scope-ref>
```

This answers the obvious objection before you raise it: *"the release only just
rolled out, of course the count is lower."* Low adoption is already priced in.
If only a tenth of your users are on 15.7.5, the new release's total volume is a
tenth as large, and `expected` shrinks by the same tenth. The percentage is a
share-against-share comparison, which is why the line calls it
*share-for-share*.

Two consequences follow from measuring one release's cohort over the rollout
window rather than a scope over a bucket:

- **The link goes to the delivery, not to a monitoring page.** There is no
  monitoring view that can reproduce these numbers: the event's chart shows all
  versions over its own range, scored against the seasonal baseline — a
  different numerator, denominator, window and estimator. So `details:` opens
  this delivery's own row in **Observe → Alerting → Delivery log**
  (`/p/<slug>/alerting`), expanded, with
  the exact scope, actual, expected and percentage the message quoted. Those are
  read back from the delivery's frozen record, so the page can never drift from
  the message, and the link keeps working after the next release ships. Release
  regression is the only item type that links there — every other scope has a
  page that shows *more* than its alert line did, and gets sent to that instead.
  The Inbox row for the same incident offers a separate link beside the scope
  name — **view event volume**, or **view event type volume** when the
  regression was found on a type. That is navigation to the event or event type
  it names, not a second route to these numbers: this delivery row is still the
  only surface that holds them, and the message still carries exactly one link.
- **Each line links to its own row, not just to the delivery.** One delivery
  carries up to 8 items, so the `?item=` on the end of the link names the scope
  that line was about: the delivery table scrolls to that row and marks it **from
  your alert**. Without it, eight lines of one message would carry the same URL
  and you would land on eight rows with nothing saying which one you clicked.
  The rest of the delivery stays on screen, so the co-firing scopes are still
  there to read. An older link, or one whose row no longer exists, still opens
  the delivery — it just marks nothing.
- **There is no recent-trend sparkline.** The only trend available is the
  event's all-versions volume over a different window — a glyph that would rise
  while the line above it says the event dropped.

The **By version** tab on an event's monitoring page shows the same check for
the *current* latest release, with the comparability verdict; see
[Release regression](anomaly-detection.md#release-regression).

### The Inbox — one row per incident {#the-inbox}

Each incident card leads with its scope and a signed delta badge (`+203%`,
`−48%`), then when it last fired as a relative time, with **Acknowledge** among
its actions. A project with no alert rules shows **No alert rules yet** in place
of the list, and its **Create a rule** button opens the rule form on the Rules
tab (`?section=monitors&new=rule`).

The top bar's bell, titled **Alerts**, previews the same queue. Its badge counts
open incidents — the same number as the sidebar's Alerting badge. The popover
lists **Open incidents** first, each linking to its Inbox card; then **Active
signals** (the four largest, then **+N more** to Anomalies); then **Recent alert
deliveries**, each reading like *Failed · Slack · 3 matched · 2h ago* and
opening that delivery. Retrying a Jira or Linear delivery from the popover asks
first, since a retry can open a second ticket. Its footer links **All anomalies
→** and **Alert inbox →**.

Outside a project — on **All projects** and the other workspace pages — the bell
covers the whole workspace. Its badge counts every project's open incidents, and
the popover lists **Projects needing attention**, one row per project with open
incidents or signals, worst first (*Checkout app · 1 open incident · 3
signals*). A project with an open incident opens its Inbox; one with only
signals opens its Anomalies list, since it has no incident to act on. The footer
links **All projects →**.

The **Inbox** is one row per **incident** — a rule firing in one direction on one
scope of a scan, or on a project-wide catalog metric — over the last 30 days,
give or take the two exceptions under
[Finding and reading an incident row](#finding-an-incident): a still-silenced
incident is held past that window, and a very loud project can get less than it.
An incident stays the same row for as
long as it keeps firing, however many buckets it spans, so a decision you make
about it holds. From the Inbox you can **acknowledge**, **resolve**, **mute**,
**reopen**, mark it a **false positive**, or save a **note** — six actions, and
`note` is one of them in its own right. Every action can carry a note alongside
it, but you no longer have to change an incident's status to write one down:
saying why something was a false positive used to mean first undoing the false
positive.

The row counts matched **items**, including repeat firings of the same scope.
Its scope names are distinct and show at most eight names; the adjacent
“distinct scope names shown” count describes that displayed list. For example,
eight items beside four names means some scopes fired more than once.

### Silencing an incident: acknowledge, mute, resolve, false positive {#silencing-an-incident}

**Four of the six actions stop further deliveries** for that incident:
**acknowledge**, **resolve**, **mute** and **false positive**. Acknowledge is in
that list, which surprises people who read it as a receipt rather than as a
silencer — and it was one, until an operator who had acked an incident and was
still being paged for it every hour reported the Inbox as decorative, which it
then was, because ack was the single action with no effect on delivery at all. A
suppressed incident produces no delivery: it is dropped before the messages are
built, not built and then withheld. **Reopen** lifts any of the four by hand, and
a **note** on its own decides nothing.

Because the row is per scope, silencing one screen leaves every other scope the
rule watches alerting normally. An incident is keyed by *(scan, rule, scope,
direction)*, so two rules watching the same event keep two incidents — silence
one and the other still pages you, from its own row.

**A catalog metric names no scan in that key**, because it is measured once for
the whole project rather than by any one scan. It therefore has a single incident
however many scans the project runs: acknowledge, mute or resolve it and the
decision holds for all of them, rather than for the scan that happened to be
collecting when it fired.

**The four do not last alike, and that is the whole answer to "I acknowledged it
and it fired again".** Acknowledge, resolve and false positive last exactly as
long as the incident does: the first collection in which that scope stops firing
returns the row to `open` by itself, so the next occurrence is a *new* incident
and alerts. That is deliberate — an old decision can never silence a new problem.
A **mute** is the single exception. It holds for the whole duration you chose
whatever the signal does in between, because *acknowledged* means "I am on this
incident" and dies with it, while *muted until Thursday* means "do not tell me
before Thursday". Releasing mutes on the first quiet collection once killed a
seven-day mute and paged its owner again hours later.

**Mute durations.** The Inbox offers **1h**, **24h**, **7d** and
**indefinitely**, with the duration written on the button, so no mute is silent
about how long it is — an unlabelled Mute button that quietly meant a week, and
quietly extended by another week when clicked again, is what made the labels
necessary. The first three are resolved into a `muted_until` instant at the moment
you click, and the row counts as `open` again on its own once it passes.
**Indefinitely** stores no `muted_until` at all: it never lapses, it is not
released when the incident ends, and the only thing that lifts it is **Reopen**.

**A mute has to end in the future.** The buttons can only ever produce one that
does, but the API takes the instant you send it, and an instant already behind
the clock is now refused rather than stored — the same refusal muting a **rule**
has always given, so the two Mute buttons no longer disagree about it. A silence
that ended before it began would have left the incident reading `open` the
moment it was written: accepted, recorded, and silencing nothing. The reverse
mismatch is refused too: a `muted_until` sent with any action other than
**mute** (acknowledge, resolve, reopen, …) returns `422` instead of being
silently dropped, on the single-incident and the bulk routes alike. The same
rule applies to `snoozed_until` on schema-drift, variable-value-drift and
comment-thread actions: it is accepted only with **snooze**.

**A rule has 1h / 24h / 7d and no indefinite option**, on purpose. Muting a rule
silences every scope it watches, not one, and a rule you never want to hear from
again is not a muted rule — it is a disabled one. The **enable** switch on the
Rules tab is that lever, and it is the honest one: a permanently muted rule
would sit in the list reading *healthy, just quiet*.

Which lever fits which intent:

| What you actually mean | Reach for | How long it holds | What else it does |
|---|---|---|---|
| I am on this — stop paging me while I work | **Acknowledge** | Until this incident ends | Stamps the row *handled by you* |
| This is over | **Resolve** | Until this incident ends | Same suppression; a different statement to whoever reads the row next |
| Do not tell me before *T*, whatever the signal does | **Mute 1h / 24h / 7d** | Exactly that long | The only decision that outlives the incident |
| Do not tell me until I say so | **Mute indefinitely** (Inbox only) | Until you press **Reopen** | Outlives everything except Reopen |
| The detector is wrong about this scope | **False positive** | Until this incident ends | **Permanently** raises that scope's `sigma_threshold` (+0.5, capped at 10) and `min_expected_count` (+5, capped at 1000), compounding on repeat clicks. It never decays; it is listed and removable under **Settings → Monitoring → Scope overrides**. Volume scopes only — on a schema drift, distribution drift or release regression it suppresses like an acknowledge and tunes nothing, because those are not scored by these per-scope knobs (a release regression does read the project-wide `sigma_threshold`, but never a scope override), and the confirmation says how many scopes it actually tightened |
| This event must never reach this channel again | A rule **filter**, `event` `not_in` […] | Permanent, per rule, no expiry | Excludes that event's *own* signals only. The project-total and event-type rollups it feeds carry no `event_id`, and a filter on a field a signal does not carry passes through — so those keep alerting. An `event_type` filter reaches the same rows from the other side: an event-anchored signal is narrowed by its event's type, resolved at match time, even though the stored row's own `event_type_id` is NULL |
| This event should not be monitored at all | **Archive** the event | Until you un-archive it | Takes it out of detection entirely: no metric points scored, no signals raised, so there is nothing left to alert on |

Read that table as "how permanent do you want this to be". The common mistake is
reaching for **mute** when the honest answer is one of the last three: a mute buys
silence and changes nothing, so whatever made the alert fire is still there,
unchanged, when the mute lifts.

:::tip It fired again — did it come back, or did it never stop?
Expand the incident row and read its deliveries. A *different* row with a later
first delivery means the scope genuinely recovered and relapsed, and your
acknowledge did its job for the incident it was about. The *same* row still open
means the suppression was lifted — by **Reopen**, or by a mute running out. A
second row for the same scope under a different rule name means two rules are
watching it and you silenced one of them.
:::

### Signal verdicts and the incident {#signal-verdicts-and-incidents}

A signal can also be triaged where you investigate it — its Anomalies row or its
drilldown — by giving it a **verdict**: **expected** (with a reason:
campaign, release, seasonality or other), **tracking bug**, **false positive**
or **real issue**, each with an optional note and the author and time recorded.
[Verdicts](./anomaly-detection.md#signal-verdicts) describes them in full.

When the signal belongs to an incident, **the incident is the source of truth**.
A verdict set on the signal is applied to the incident rather than stored beside
it:

| Verdict on the signal | Incident status |
|---|---|
| False positive | `false_positive` — tunes the scope exactly as the Inbox action does |
| Real issue | `acknowledged` |
| Tracking bug | `acknowledged` |
| Expected | `resolved` |

So each of these stops further deliveries for that incident, and lasts as long as
the incident does, like the Inbox action it maps to. The signal, in turn, shows
the incident's state: acknowledge, resolve or mark the incident a false positive
in the Inbox and the signal's verdict display follows, marked as coming from the
incident. A signal no rule routed has no incident and reads **Not routed** on
its drilldown signal card; its verdict is stored on the signal itself.

Three edges keep the two sides in step:

- **Clearing the verdict on a routed signal reopens its incident**, so the UI
  asks for confirmation first. False-positive tuning stays, as with reopening in
  the Inbox.
- **An Inbox action that moves the incident drops signal verdicts that no longer
  agree** with the new status; one that still agrees (tracking bug on an
  acknowledged incident, say) stays as the detail.
- **A verdict set before the signal was routed carries over**: the signal keeps
  it, and the new incident's status is left untouched.

A false positive tunes detection once whichever side it is set from — marking
the signal a false positive is the Inbox's **False positive** action, not a
second ratchet step on top of it.

### Notes on an incident {#incident-notes}

The note is attached to the incident, survives later actions, and is only
replaced when you write a new one.

**A note-only save is the one action that decides nothing**, and it is treated
that way: it does not change the incident's status and it does not stamp who
handled it or when, so the row does not start claiming "already handled by
you". The other five actions do stamp it, which is what the row's *handled by*
line is read from — and what tells a re-fired incident from a fresh one.

**Add note** opens the box with the cursor already in it, and **Ctrl+Enter**
(**⌘+Enter** on a Mac) saves without reaching for the button. Enter itself makes
a new line: a note is prose. The box holds 2000 characters and says so once you
are inside the last 200, because past the limit it simply stops accepting what
you type.

**Emptying the box and pressing Clear note deletes the stored note.** The button
renames itself to say so, since "Save note" over an empty box reads as doing
nothing. Taking any *other* action with an empty box leaves the stored note
alone, so acknowledging an incident never quietly erases what someone wrote on
it earlier.

### Acting on several incidents at once {#bulk-actions}

A bad deploy leaves the Inbox holding twenty rows that all say the same thing,
and the decision about them is one decision. Each incident carries a
**checkbox**, and ticking any of them raises a bar at the bottom of the list
reading **N selected** — the same bar the events catalog puts there, in the same
place, so it is not a second thing to learn. The bar offers
**Acknowledge**, **Resolve**, **Reopen** and **Note**, plus **Mute** with the
same **1h / 24h / 7d / indefinitely** durations a single incident offers.
Applying one needs the **editor** role, like every other action in the Inbox.

**Two ways to build a selection faster than one tick at a time.** The Inbox
panel header carries a **Select all N shown** checkbox, where N is the number of
cards rendered underneath it — never the project's total, so what the bar is
about to act on is on screen and countable. It shows a half-tick when only some
of those cards are selected, and clearing it deselects the same set. Separately,
**shift-clicking** an incident's checkbox extends the selection from the last
checkbox you touched to the one you just clicked, and the whole run takes the
state that clicked box moved to: shift-click a ticked box to clear a run,
an unticked one to fill it. Both ends of a range are on screen and both were
chosen by hand, so neither control can put an incident you have not seen into
the selection — which is the same line the bar draws by refusing "select all N
matching".

These are not new levers, which is why the table above has no bulk row: a bulk
acknowledge is an acknowledge, holding exactly as long and stamping exactly what
it stamps, done to twenty incidents instead of one. Read that table for *which*
decision you mean; the selection only decides how many rows it lands on.

**False positive is not on the bar, deliberately and permanently.** Direction is
part of an incident's key, so one scope spiking and that same scope dropping are
two rows in this list — and marking both would ratchet that scope's
`sigma_threshold` and `min_expected_count` **twice** for a single human
judgement, permanently and compounding, with nothing in the record to say the
two nudges were one click. It stays on each incident's own action row, where the
scope it will tune is in front of you. The server refuses it in bulk with a
**422** even when something other than the UI asks.

**A bulk note is copied into each incident, not shared between them.** There is
no group object behind a selection: afterwards each of the twenty rows shows the
same note text, the same *handled by* and the same status, but as twenty
independent copies, exactly as if you had typed it twenty times. Changing your
mind later therefore means selecting those same incidents again and writing a
new note: there is no one note to edit, and a reader who assumes there is finds
out weeks later, editing one row and wondering why the other nineteen still say
the old thing.

**Note** on the bar opens a box that works two ways. **Save note** writes it and
moves nothing, and pressing any other action *while it holds text* sends the note
with that action — one request, so "mute these and say why" costs one click, not
two. The box stays open while it has text in it, precisely so a note can never
ride along invisibly, and it empties when the selection does: a sentence written
about one batch is not a sentence about the next one.

**Clearing is not offered in bulk.** An empty box on the bar means *no note*, not
*erase theirs* — the selected incidents may each carry a different note, and none
of them is on screen to be looked at first. Delete a note on the incident's own
row, where the note you are about to lose is in front of you.

**A bulk mute asks before it silences anything**, naming how many incidents are
about to go quiet. A mute is the one decision that outlives its incident, so
twenty of them is the mistake worth spending a confirmation on.

**The selection stops at 200 incidents.** Past that the bar's actions switch off
and it says how many to untick, rather than leaving a button that would be
refused.

**It applies to the whole selection or to none of it.** Every id is validated
before anything is changed, so a selection carrying an id this project does not
have writes nothing at all — never eleven incidents acted on and nine not, which
is the state the list can no longer tell you apart afterwards.

**The audit log still keeps one row per incident.** A bulk action writes an
audit entry for each incident it touched, all sharing one **batch id**, so the
log can answer *which* incident was muted and by whom — a single row saying
twenty were muted could not — while the batch id groups them back into the one
click they were.

`POST /api/v1/projects/{slug}/alert-inbox/bulk-actions` takes
`correlation_group_ids` alongside the same action fields as the single-incident
route, and answers with the rebuilt incident cards, the batch id, and
`overrides_written` — always `null` here, since the one action that writes
overrides is the one this route refuses. It deliberately returns a body where
most bulk routes in this API answer `204`, matching the single-incident action,
which likewise gives you back the card you were looking at when you pressed the
button.

### Finding and reading an incident row {#finding-an-incident}

The scope name on an incident row links to the thing that fired — the event,
event type, project-total or metric monitoring page — so you can check whether
the alert is real without leaving for the catalog and finding it by hand. Scopes
with no page of their own (a schema or distribution drift) link to the event they
were detected on, or stay plain text when there is nothing to open.

A **release regression** is the one scope whose name stays plain text even
though it names something you can open. No page corroborates the comparison it
made — see [Release-regression items](#release-regression-items) — so linking
the name would offer a chart that disagrees with the alert as if it were the
proof. The row instead offers a separate link beside the name, worded for what
it opens: **view event volume** when the regression was found on an event,
**view event type volume** when it was found on an event *type*. Either way it
is navigation, not evidence. It opens that entity's own monitoring page, which
charts its volume against the seasonal baseline over that page's own range; the
page's **By version** tab covers the current latest release only, so it will not
show this incident's comparison once a newer release ships.

**The 30 days are a rolling window, not a backlog.** An incident nobody touches
is not resolved when it leaves the page — it simply stops having fired inside the
window, and the row disappears with no action recorded against it and no state
change to explain it. The list's `total` counts what is inside the window (and
matching the status filter), so it is "incidents to triage now", never "incidents
this project has ever had".

**One exception, and it exists because the window would otherwise swallow your own
decisions.** An incident that is *still silenced* — effective status not `open` —
is held in the list however long ago it last fired. Silencing an incident is the
act of stopping its deliveries, and the window is a window on deliveries; without
this, a muted incident would leave the page exactly 30 days after you muted it
while its suppression carried on being enforced forever — and the only Unmute
control lives on the row that vanished. A mute that has *run out* gets no such
treatment: it is `open` again, so it stopped being a decision. The number held
past the window is capped per status, so this is a safety net under decisions
somebody made by hand and not a second, unbounded inbox. The page says
`last 30 days + still silenced` for the same reason.

**A second bound, which normally never bites.** The list also reads at most a
fixed number of alert rows per project — 2,000 — newest first, and that cap is
applied *before* incidents are grouped. A project loud enough to exceed it gets
a window shorter than 30 days, and the incidents that fall off would otherwise
be indistinguishable from ones somebody had dealt with. So when it happens the
page stops saying `last 30 days`: the subtitle names the date the list really
starts at, and a line at the top of the list says why and what is missing. It is
not a state anything measured has been near — the busiest project on record uses
about an eighth of the cap — but it is the one shortening of the window nobody
asked for, so it is never silent. Those incidents still open from their own
links, and are still held here if they are still silenced.

What they do **not** do is keep counting. The sidebar's open-incident badge is
computed over the same capped row set, so an incident the cap dropped leaves the
badge as well as the list. Nothing about the incident changed — it was not
resolved and nobody handled it — but the number beside **Alerting** stops
including it, and that is the one respect in which a shortened window is lossy
rather than merely narrower.

**Open incidents sort above handled ones.** Effective openness is the list's
primary ordering term, so something nobody has dealt with can never be pushed off
page one by something already triaged; within each run the newest activity leads,
tie-broken on the incident id so paging cannot show one incident twice or skip it.
A muted, resolved, acknowledged or false-positive incident is therefore reached
with the **status** filter — `?status=open` / `acknowledged` / `resolved` /
`muted` / `false_positive`, and an unrecognised value is a 422 rather than a
silent empty page — and not by scrolling. A mute that has run out counts as
`open` again and rejoins the top run on its own; an indefinite mute never runs
out, so it stays under `?status=muted` until you reopen it.

**The Inbox opens on Open.** With no `?status=` in the address the page lists
open incidents only — the triage queue, not every incident in the window — and
that default is not counted as a filter, so **Clear filters** does not show on
first load. **All** is an explicit `?status=all`.

**The filter is in the page URL as well.** Picking a status writes
`?status=<acknowledged|muted|resolved|false_positive|all>` onto
`/p/<slug>/alerting`, beside `?section=` and `?scan=` (Open, the
default, drops the parameter). So a filtered queue can be bookmarked or pasted
to a colleague, and opening an incident to check the scope that fired — a page
off this route entirely — and pressing Back returns the queue you were working
rather than all of them again. **Clear filters** returns to the default Open
queue; an empty result's **Show all** asks for every status. Unlike the API, a
value the page does not recognise degrades quietly to **All**, the same rule
`?section=` and `?scan=` already follow.

**Status is not the only filter.** A bar above the list narrows it four more
ways, each of them a field the cards already show, and each of them in the URL
beside `?status=`:

| Control | Parameter | What it matches |
| --- | --- | --- |
| Last fired (one chip; its From / To open the app's calendar picker in a popover) | `?fired_from=`, `?fired_to=` (`YYYY-MM-DD`) | When the incident **last** spoke, not when it started. Both ends are inclusive whole days, read in your own timezone, so "to the 8th" includes that evening. |
| Kind | `?scope_type=` | Any scope the incident fired on — an incident holding one release regression among ten volume firings is found by either. |
| Direction | `?direction=drop` / `spike` | The direction of its **newest** firing, which is the one the card shows. |
| Scope | `?scope=` | Case-insensitive substring of any scope name or reference in the incident, including the ones past the eight the card lists. |

The date filter **narrows the 30 days the list already covers**; it cannot fetch
an incident older than that, and the bar says so under the inputs. Combined with
a status, the filters read as "and": `?status=open&direction=drop&scope=checkout`
is the open drops on checkout. The count under the list and the "of N" beside it
both describe the filtered set, so the number above the cards always counts the
cards. Each option of the **Status** filter carries its own count ("Open · 3"):
the response's `status_counts` counts every status after the other filters and
before the status one, so an option says what picking it would list. An unrecognised `scope_type` or `direction` is a 422 from the API, while
the page — like `?status=` — drops it quietly rather than turning a stale link
into a failed request.

The Inbox lists the last **30 days**, but an alert's link is not bound by that
window: `GET /api/v1/projects/{slug}/alert-inbox/{correlation_group_id}` resolves
one incident by id and deliberately ignores the lookback, because the reader
opens the link late and would otherwise land on a page of twenty unrelated
incidents. Read that way, an incident also reports its **whole** history — its
true first delivery, and its full item and delivery counts — where the same row
in the list describes only the part inside the window. Acting on an incident
older than 30 days works too, and gives you back the same card you were looking
at when you pressed the button.

Each incident row also carries **what was sent** for it: expand it to see that
incident's deliveries — destination, status, and the item lines the message
quoted — without leaving the row whose buttons you are about to press. **The
link in an alert opens exactly this**, whatever fired it, with the incident
expanded and the quoted line highlighted. Previously only release regressions
reached this page and everything else linked to the event's monitoring page,
which shows neither the deliveries nor the actions.

The **Delivery log** panel below stays the whole-project delivery list, filterable by
status, channel, destination, rule, scan and a **Sent** date chip whose From / To
open the app's calendar picker (the table
itself has no Channel column; each row's destination names it) — the view for "did anything fail to
go out", rather than for acting on one incident. Deliveries too old to belong to
an incident (written before incidents existed) appear only there.

Like the Inbox's, the Delivery log's filters and its page are kept in the URL
(`delivery_status`, `delivery_channel`, `delivery_destination`, `delivery_rule`,
`delivery_from`, `delivery_to` and `delivery_offset`, beside `scan`), so a
filtered log survives **Back** and can be shared as a link. Expanding a failed
delivery shows its full error message first.

### What an incident row carries

`GET /api/v1/projects/{slug}/alert-inbox` returns these rows as typed objects.

It pages two ways. `offset` and `limit` work as before; the response also
carries `next_cursor` — an opaque string, `null` on the last page — and passing
it back as `?cursor=` continues strictly after the last row served. Prefer the
cursor for "load more": an incident acknowledged on page 1 between two requests
sorts down past the page boundary, and with an offset the row after it is
skipped, never served. `GET /alert-deliveries` pages the same way. `cursor` and
a non-zero `offset` together are a 422, as is a cursor that does not decode.

The fields worth knowing before you read one:

| Field | Means |
|---|---|
| `first_delivery_at` | When the incident first fired **inside the window this reading covers**. `latest_delivery_at` says when it last spoke and nothing about how long it has been going, which is the difference between a blip and a week-old regression. |
| `actual_count`, `expected_count`, `percent_delta` | The size of the **newest** item in the incident, so the row can state a magnitude without expanding its deliveries. |
| `max_abs_percent_delta` | The largest deviation **anywhere** in the incident, so "worst first" is orderable without fetching its items. |
| `scope_type`, `scope_ref`, `event_id` | The newest item's scope in routable form — what the row's scope link opens. Always sent; `event_id` may be `null`. |
| `scope_types` | The **distinct** scope kinds present, sorted. |
| `rules` | Every rule behind the incident, as `{id, name}` pairs sorted by name. |
| `rule_names` | The same names as plain sorted text, which is what the row renders as its label line. |
| `acted_by`, `acted_by_name` | Who last acted on it, and their display name. |
| `muted`, `muted_until` | Whether it is silenced, and until when. `muted_until` is `null` both when the incident is not muted and when it is muted **indefinitely**, which has no end to report — so read `muted` for the fact and `muted_until` only for the deadline. |

Several of those need a sentence more.

:::warning `first_delivery_at` is windowed on the list
On the list — and on an action's reply, which rebuilds the same card from the
same rows — `first_delivery_at` is the first delivery **within the last 30
days**, so an incident older than that reports a first delivery inside the window
rather than its true birth. `item_count`, `delivery_count` and
`max_abs_percent_delta` are qualified exactly the same way.
`GET /alert-inbox/{correlation_group_id}` reads the whole history and does report
the true first — one of the reasons that route exists.
:::

**`percent_delta` is `null`, not `0`, when `expected_count` is `0`** — the same
encoding [the delivery's `items[]` already uses](#what-a-webhook-destination-posts),
enforced the same way, because one incident may not answer the same question two
ways depending on which payload you read it from. That includes the negative
case: a signed metric's non-zero baseline is a baseline here too, and its
`percent_delta` is the same magnitude-based size. `max_abs_percent_delta` is
computed over the rows that **have** a baseline — a non-zero `expected_count`,
negative included — and is `null` when no row in the incident does: a group made
entirely of zero-baseline firings has no measured deviation to be the largest,
and reporting `0.0` there sorted the loudest incidents last. It is already an
absolute value, so it too states a size and not a direction. Use
`absolute_delta` on the items for the no-baseline class.

**`scope_types` exists because `scope_type` is the newest item's alone.** An older
incident can mix kinds, so one value cannot label the row — nor tell a client what
a **false positive** click will actually tune, since only some scope kinds are
ratchetable — see the false-positive note that closes this section.

**`acted_by_name` is `null` when that user has no name on file**, and it
deliberately does *not* fall back to their email address. Every project member can
read the Inbox, and a fallback would turn incident rows into a roster of
colleagues' email addresses on a surface that previously exposed nothing but an
opaque id. The row says *handled* without a name.

:::note Why `rules` replaced `rule_ids` and `rule_names` as identifiers
The two parallel arrays could not be zipped: `rule_ids` was sorted by UUID and
`rule_names` by name, so index *i* of one had nothing to do with index *i* of the
other, and a row linked "Volume rule" to whichever monitor happened to sort first.
Two rules of one incident can even share a name, so no client-side join could
repair it either. `rules` carries the id and the name **together**, sorted by name.
`rule_names` is still sent, as the label line's plain text.
:::

:::note An incident's `muted` and a rule's `muted` are not built alike
On an incident, `muted` is true exactly while the mute is in force **and**
`muted_until` is nulled the moment it lapses, so the pair can never say the mute
is still running when it is not. An **indefinite** mute is the one case where
`muted` is true with `muted_until` `null` — there is no lapse instant because
there is no lapse. On a rule (and on a monitor) `muted` is likewise the effective flag, but
`muted_until` is the raw stored timestamp and keeps being emitted after it has
passed — see
[What a rule reports about its own state](#what-a-rule-reports-about-its-own-state).
Read `muted` in both.
:::

:::note
Marking a group **false positive** doesn't just hide it — on the scopes that are
scored by the two numeric detector knobs it nudges the detector on **the scope it
fired on** (raises that scope's sensitivity threshold and minimum expected count)
so the same benign pattern is less likely to alert again. Every other scope keeps
the sensitivity you configured, and the project-wide settings are not touched.
The nudge is permanent; it is listed and can be removed under **Settings →
Monitoring → Scope overrides**. See
[False positives self-tune the thresholds](./anomaly-detection.md#false-positives-self-tune-the-thresholds).

**Not every scope can be nudged.** Only **project-total**, **event-type**,
**event** and **metric** scopes are scored against a sigma threshold and a
minimum expected count, so only those are ratcheted. **Schema drift**,
**distribution drift**, **variable-value drift** and **release regressions**
reach the Inbox and can be marked a false positive — the incident is recorded and
silenced exactly as it would be — but nothing tunes them, because neither knob is
what decided they fired. Tighten those at the source instead: the drift bands
and the release-regression comparability gate, both in
[How anomaly detection works](./anomaly-detection.md).

So that the button stops promising a change it did not make, the action's reply
carries **`overrides_written`** — how many scopes were actually tightened, which
is `0` for an incident made entirely of the scope types above. It is `null` for
every other action (acknowledge, resolve, mute, reopen, note), because those
never touch detection at all; `null` means "not applicable" and `0` means "tried
and tightened nothing".
:::

## Set up your first alert

1. **Observe → Alerting → Destinations** — add a destination (e.g. a
   Slack webhook) and press **Test**, which sends one message through the real
   channel and answers whether it arrived — see
   [Testing a destination](#testing-a-destination).
2. **Add a routing rule** on that destination: choose the scope, direction(s),
   thresholds, optional filters, and cooldown.
3. *(Optional)* **Simulate** it over recent days to confirm it isn't noisy — and
   try a stricter threshold there before saving one, see
   [Replaying a what-if without saving it](#replaying-a-what-if-without-saving-it).
4. **Save.** The next scan that produces a matching signal sends a delivery and
   records it in the Inbox and Delivery log views.

The Delivery log can be filtered to a single scan with
`?scan=<scan_config_id>` — `/p/<slug>/alerting?scan=<scan_config_id>`.
That is the link behind a scan run's **Alerts queued** counter, so an alert
naming a scan is reachable from the run that queued it. An id the project does
not have degrades to **All**.

If alerts don't arrive, see
[Troubleshooting → "alerts never fire"](./troubleshooting.md). For the broader
catalog of monitoring surfaces, see the [Feature reference](./feature-reference.md).
