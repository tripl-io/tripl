---
title: Troubleshooting & FAQ
sidebar_position: 90
---

# Troubleshooting & FAQ

This page collects the failures people actually hit when running tripl, written
as **symptom → likely cause → fix** playbooks. Each cause is grounded in how the
worker, adapters, and startup checks actually behave — not guesswork.

Before you dig into a specific symptom, two facts explain most "nothing is
happening" reports:

- **The background worker and scheduler do the work, not the API.** Scans,
  metric collection, anomaly detection, and alert delivery all run as Celery
  tasks on the `celery-worker` container, dispatched on a schedule by
  `celery-beat`. If either container is down, the UI stays up but nothing
  progresses.
- **Filling the catalog and collecting metric points are two different things.**
  A run of a scan (`run_scan`) discovers events and fills the tracking plan.
  Metric points, anomalies, and alerts come from a separate, scheduled
  `collect_metrics` task, dispatched only for scans that have both a schedule and
  a time column. Starting a run does **not** record metric points by itself.

A quick health check for a Docker deployment:

```bash
docker compose ps
docker compose logs -f celery-worker
docker compose logs -f celery-beat
```

You want to see `postgres`, `rabbitmq`, `redis`, `app`, `celery-worker`, and
`celery-beat` healthy/running, and the `migrate` one-shot already exited
successfully (`Exited (0)` in `docker compose ps`).

---

## The browser repeatedly logs a realtime stream protocol error

**Symptom.** Project pages keep working, but Chrome repeatedly logs
`ERR_QUIC_PROTOCOL_ERROR 200 (OK)` for
`/api/v1/projects/{slug}/events/stream`.

**Cause.** The request is a long-lived Server-Sent Events stream. A `200`
means the response started successfully; the protocol error means an HTTP/3
edge or proxy then rejected or reset that stream. Make sure no intermediary
adds or forwards connection-specific headers such as `Connection` or
`Keep-Alive` on HTTP/2 or HTTP/3 responses.

**Fix.** Upgrade tripl to a version whose realtime endpoint does not emit
connection-specific headers, and verify the edge preserves
`Content-Type: text/event-stream`, disables response buffering, and allows a
streaming response to remain open without applying response compression. The
endpoint sends a heartbeat every 15 seconds, so an edge idle timeout comfortably
above that value should not close healthy streams. While disconnected, the
frontend falls back to polling and reconnects with exponential backoff.

If Redis is unavailable, the stream announces `backend: degraded` and remains
open with heartbeats while the browser polls. Reconnect replay subscribes before
reading the buffered events so updates published during setup are not lost.

---

## No metrics appear after a scan

**Symptom.** A scan finished and the catalog filled with events, but the
monitoring charts stay empty and no anomalies or alerts ever show up.

**Likely causes.**

1. **The scan is not a monitoring scan.** The dispatcher (`check_metrics_due`)
   only ever selects configs where **both** `interval` and `time_column` are set.
   A config missing either is silently skipped — it will never collect metrics,
   only catalog events. Its runs still succeed, which is why this looks like
   nothing is wrong.
2. **`celery-beat` is not running.** Metric collection is triggered by the
   beat schedule entry `check-metrics-due`, which fires every 300 seconds. With
   no beat container, `collect_metrics` is never dispatched.
3. **The first complete bucket hasn't closed yet.** On the very first run
   (nothing collected yet) the dispatcher collects immediately; after that it
   only fires once a *new complete* interval bucket exists — the latest complete
   bucket is `floor(now) - interval`. For a 6h interval you may wait up to 6
   hours for the next point to appear. A collection that ran and found **no
   rows** counts as collected for that window, so a config whose warehouse
   window is still empty retries once per interval rather than every five
   minutes; it is not stuck.
4. **The time column doesn't actually constrain the window.** Metrics are
   bucketed on `time_column`. If the column isn't a usable timestamp in the
   warehouse, the windowed query returns nothing to bucket. A `sql` metric's
   outer SELECT must also name both its `time_column` and its value column —
   `value_column`, or `value` when that is left unset — or saving it is
   refused with a message naming the missing column.
5. **The worker can't reach the warehouse.** `collect_metrics` connects to your
   data source the same way a scan does; a broken connection fails the run (see
   [A scan run fails](#a-scan-run-fails--a-data-source-connection-test-fails)).
6. **A catalog metric's value is NULL for that bucket.** An `avg` over a column
   that is all NULL for the window, a `sum(x) / nullif(count(*), 0)`, or an
   unmatched `LEFT JOIN` all project NULL. That bucket is recorded as *absent* —
   a gap in the chart, not a `0` — and the run still succeeds, on a `sql` metric
   and a `fact` metric alike; the SQL metric preview draws the same gap. If you
   meant zero, wrap the measure in `coalesce(…, 0)`. A value cell that is not
   NULL but is not a number either — a value column projected as text — is still
   an error.
7. **The source backfilled buckets older than the window a scheduled run
   reads.** Scheduled collection does not re-read history: it resumes two
   intervals back from wherever collection actually got to and runs to the
   latest complete bucket — three intervals wide in steady state, thirty on a
   scan's very first run. A source that stopped for a while and later wrote rows
   into *old* buckets has put them behind that trailing slice, so no scheduled
   run will ever pick them up. Fill them with **Replay a period…** (in the scan
   page header) over the period they belong to.

**Fix.**

- Open **Govern → Scans** and look at the badge on the scan's row. **Catalog
  only** or **Needs a time column** means this scan was never going to produce a
  metric point, and the fix is in the form, not in the infrastructure.
- Open the scan and check **What this scan does** at the top of the form. Choose
  **Catalog + monitoring**, then fill in the **Time column** and **Schedule** it
  asks for — the form refuses to save until both are answered. Save, then wait
  one beat cycle (≤ 5 minutes) for the first collection, or fill a past window
  right away with **Replay a period…** in the scan page header, which opens a
  dialog (it needs the same time column and schedule, so it unlocks with them).
- If the scan is deliberately **Catalog only**, nothing is broken: that mode
  records no metric points by design, so it raises no anomalies and sends no
  alerts.
- Confirm beat is alive:

  ```bash
  docker compose logs --tail=50 celery-beat
  docker compose logs --tail=100 celery-worker | grep check_metrics_due
  ```

  You should see lines like `check_metrics_due: N configs checked, M dispatched`
  and `Dispatching collect_metrics for '<name>' (interval=...)`.
- If you just created the config and it's a long interval, give it one full
  interval before expecting a second data point.

:::note
Each scan runs at most one active collection at a time. If a previous run is
genuinely stuck (worker OOM/redeploy with no heartbeat), the dispatcher marks it
failed after **75 minutes** without progress and lets the next run proceed — so a
wedged run self-heals within that window rather than blocking collection forever.

A reaped collection run **stays failed**. If its queued message arrives late, or
the original task was alive all along and finishes after the reap, it does not
re-open the row: the replacement run owns that window, and a late success would
otherwise erase a failure the dispatcher's backoff had already counted. Nothing
it managed to do is discarded — the metric points it wrote are kept and its run
report is still filled in — only the verdict stands as *failed*.
:::

---

## Alerts never fire

**Symptom.** Anomalies show up in the monitoring view (or you expect them to),
but no Slack/Telegram/email/webhook/Jira/Linear notification ever arrives.

The delivery chain is: `collect_metrics` → recalculate anomalies → match rules
and create `AlertDelivery` rows (`pending`) → `send_alert_delivery` actually
sends. A break anywhere in that chain produces silence.

**Likely causes & fixes.**

1. **No anomaly was detected.** Detection is statistical, not a fixed
   threshold. A series needs enough history before the seasonal (phase) baseline
   engages — until then it falls back to a rolling baseline that itself needs
   `min_history_buckets` of data, and low-volume series below `min_expected_count`
   are skipped entirely so noise doesn't flood you. Brand-new scans, sparse data,
   or a too-high `sigma_threshold` all legitimately produce zero anomalies.
   Review the project's anomaly settings (baseline window, sigma threshold,
   minimum expected count) and let more history accumulate. If you have marked
   incidents on this scope a **false positive**, check **Settings → Monitoring →
   Scope overrides** too: each click permanently raised that scope's
   `sigma_threshold` and `min_expected_count`, and the ratchet never decays — see
   [False positives self-tune the thresholds](anomaly-detection.md#false-positives-self-tune-the-thresholds).
2. **No enabled alert destination.** If the project has no destinations, or
   none are enabled, no deliveries are created. Add and enable a destination.
3. **No enabled rule, or the rule doesn't match.** A destination with no
   enabled rules is skipped. A rule only fires for anomalies it matches
   (by scope/direction/etc.). Check the rule is enabled and its scope covers the
   anomaly you expect.
4. **Cooldown, or a decision you made in the Inbox.** A rule won't re-notify
   the same scope until its `cooldown_minutes` elapses. Separately, an incident
   you marked **acknowledged**, **resolved**, **false positive** or **muted**
   produces no further deliveries at all — it is dropped before any message is
   built. All **four** suppress. **Acknowledged** is the one people forget,
   because it reads like a receipt; it is not, and an acknowledged incident is a
   common answer to "the rule is on, the destination is on, and nothing
   arrives". Suppression is per incident, meaning per *(scan, rule, scope,
   direction)*, so it silences one scope of one rule and nothing else.

   They do not expire alike. **Acknowledged**, **resolved** and **false
   positive** last only as long as the incident: the first collection in which
   that scope stops firing puts the row back to `open`, so the next occurrence
   is a *new* incident and alerts normally. A **mute** is the exception — it
   holds for its full `muted_until` whatever the signal does in between, and an
   **indefinite** mute (Inbox only) holds until you press **Reopen**. A lapsed
   mute counts as `open` again on its own.

   Open incidents sort above handled ones, so a silenced row is reached with the
   **status** filter (`?status=acknowledged` / `resolved` / `muted` /
   `false_positive`) rather than by scrolling. **Reopen** lifts any of the four.
   Which decision you actually wanted is
   [Silencing an incident](alerting.md#silencing-an-incident).
5. **Email destination but SMTP isn't configured.** Email sends fail with:
   *"Email destination is configured but SMTP is not — set SMTP_HOST (and
   SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth)."* Set `SMTP_HOST`,
   plus `SMTP_FROM_ADDRESS` or a per-destination From: address. The worker reads
   SMTP settings at send time, so a config change takes effect without
   recreating the destination. The default From: may carry a display name
   (`Tripl Alerts <no-reply@example.com>`), so a send that failed with *"From:
   address is invalid"* means the value has no usable address in it — stripping
   the name is not the fix. A per-destination override is checked the same
   way and may carry a display name too.
6. **The port and the security mode disagree.** The symptom is a ten-second
   pause followed by *"Connection unexpectedly closed: timed out"*, which looks
   like the relay is down when it is answering perfectly well. Port 465 is
   implicit TLS — the server expects a TLS handshake immediately and never sends
   the plaintext greeting a STARTTLS client waits for. Set **Security** to
   *Implicit TLS* for 465, or *STARTTLS* for 587/2525. Settings → Email →
   **Send test email** reproduces it in one click and prints what the relay
   said. This is also the failure that silently breaks password-reset links,
   since a failed reset send is invisible to the person who requested it.
7. **Destination credentials are invalid.** Slack/Telegram/webhook/Jira/Linear
   each re-validate their secret at send time; on failure the delivery is marked
   `failed` with a message like *"Slack destination configuration is invalid.
   Update the webhook URL."* Check the failed delivery's error in the UI/logs.
8. **The delivery was stranded.** If the worker died between creating the
   `pending` delivery and dispatching it, or the broker was down at dispatch, a
   maintenance task (`requeue_stranded_alert_deliveries`, every 5 minutes)
   re-enqueues deliveries still `pending` after 15 minutes, up to 5 attempts,
   then marks them `failed`. The same task retries a delivery that already
   `failed` when its stored error is a transient network failure — destination
   unreachable, connection refused, a timeout — a few times, minutes apart,
   within the same attempt budget; only failures from the last six hours are
   picked up. While an attempt is queued the row shows `pending` but keeps its
   last error; a failed attempt returns it to `failed` with the fresh error,
   and a success flips it to `sent`. Jira and Linear deliveries and disabled
   destinations are never auto-retried. A stranded delivery whose destination
   has since been disabled is re-enqueued once and then marked `failed` with
   *"Alert destination ... is disabled: alerts are not routed here. Nothing was
   sent."* — it is not delivered. Re-enable the destination and press **Retry**
   if you still want it.
   Any other failure (bad credentials, a rejected payload) is never retried
   automatically: fix the cause and press **Retry**, which also resets the
   attempt budget. A permanently failing delivery will eventually stop cycling
   and show as failed.
9. **A drift scope is on but nothing feeds it.** The two drift scopes act on
   signals another part of the project has to produce first, so a rule can have
   one enabled and be structurally unable to fire. **Property value drift**
   needs some property to document an allowed-values list on the **main**
   branch — the property's own list or a per-event override, either one is
   enough — which you supply under **Properties**, *or* a value drift already
   collected in the project (an open or snoozed row from the last 30 days);
   values documented on a working branch do not count until it merges. **Distribution drift** needs a scan that
   names the columns to watch (**Scan settings → Distribution drift**), or a
   **significant** drift already collected in the project — stable and minor
   scores do not count, because no alert can ever be built from them. The
   monitor's own screens now say this out loud: the rule editor and the monitor
   detail mark such a scope inline and
   link to the screen that supplies the missing data. The check behind those
   notices is project-wide, so a rule bound to a single scan shows no warning as
   long as *some* scan watches a column — see
   [When a scope is on but nothing feeds it](alerting.md#when-a-scope-is-on-but-nothing-feeds-it).

**Fix workflow.**

```bash
# collect_metrics logs its result, including how many deliveries it queued:
docker compose logs --tail=200 celery-worker | grep -iE "collect_metrics|alerts_queued"
# Why did a specific send fail? The error is persisted on the delivery and logged:
docker compose logs --tail=200 celery-worker | grep "Failed to send alert delivery"
```

:::tip
Use the in-app rule simulator to confirm a rule matches a given anomaly before
blaming delivery — the simulator and the live pipeline share the same matcher
(`tripl.alerting_matching`), so if it doesn't match in the simulator it won't
match live either.
:::

---

## An incident I acknowledged fired again

**Symptom.** You acknowledged an incident in the Inbox — probably with a note
saying what it was — and hours or days later the same scope paged you again. The
obvious conclusion is that **acknowledge** does nothing, and the obvious next move
is to mute it. Both are wrong, and muting is often the wrong lever for what you
meant.

**What actually happened.** Acknowledge *does* suppress. An acknowledged incident
creates no further deliveries: it is dropped before the messages are built, not
built and quietly withheld. But the decision is scoped to that incident, and an
incident ends when its scope stops firing — at that moment the row returns to
`open` by itself. The next time the scope deviates it is a **new** incident, and a
new incident has never been acknowledged, so it alerts. That is deliberate:
whatever you decided about last Tuesday's drop must not silence next month's
outage.

So "it came back" almost always means *the problem came back*, with a quiet
stretch in between that ended your incident. Check by expanding the row: a
different row with a later first delivery is a genuine relapse; the same row still
open means somebody pressed **Reopen** or a mute lapsed; a second row under
another rule's name means two rules watch that scope and you silenced one of them.

**Which lever you want depends on what you meant.**
[Silencing an incident](alerting.md#silencing-an-incident) sets all of them side
by side; the short form:

- **"I am working on it and want quiet meanwhile."** Acknowledge was already the
  right action and nothing is broken. Re-acknowledge the new incident, or fix the
  cause.
- **"I want a guaranteed quiet window regardless of the signal."** **Mute** — 1h,
  24h, 7d, or indefinitely. A mute is the one decision that survives the incident
  ending: it holds for its whole duration whatever the data does, and an
  indefinite mute holds until you **Reopen** it. It changes nothing else, so the
  cause is still there when it lifts.
- **"The alert is wrong for this scope."** **False positive.** It silences the
  incident exactly as an acknowledge does *and* permanently raises that scope's
  `sigma_threshold` and `min_expected_count`, so the same benign pattern needs a
  bigger, higher-volume deviation to be flagged next time. Repeat clicks compound,
  the ratchet never decays, and every scope it has tightened is listed under
  **Settings → Monitoring → Scope overrides** — see
  [False positives self-tune the thresholds](anomaly-detection.md#false-positives-self-tune-the-thresholds).
  This is the lever for "the detector is wrong here", and the only one of the four
  that changes what gets detected.
- **"This event must never reach this channel again."** Add a **filter** to the
  rule — `event` `not_in` […]. It is permanent, per rule, and has no expiry. One
  catch: it excludes that event's *own* signals only. The project-total and
  event-type rollups the event contributes to carry no event id, and a filter on a
  field a signal does not carry passes through — so a rule watching project totals
  will still tell you when the total moves, event filter or not.
- **"This event should not be monitored at all."** **Archive** the event. Scans
  stop collecting for it and detection skips it on every scope it appears in, so
  there is no signal left for any rule to match. Archiving is reversible; set
  another status and collection resumes.

:::tip Acknowledge is not a weaker mute
They answer different questions. Acknowledge answers *"is somebody on this?"* and
is worth pressing even when you know it will lapse, because the row then says
**handled by you** and your colleagues stop opening the same incident. Mute
answers *"when may I be told again?"*. Pressing mute to mean "seen" loses the
first answer and buys a silence you did not want.
:::

---

## The scan preview names no events

**Symptom.** The preview loads, but **What this scan would create** lists zero
events, or far fewer than you expected.

The dry run is the same planner a real run uses, so "it would create nothing" is
a real answer, not a broken panel. Six causes, in the order worth checking:

1. **Nothing tells the scan how to name events.** A scan names its events one of
   two ways: an **Event type**, which makes every row the same event, or an
   **Event type column**, whose values become the event names. With neither, the
   panel says so — *Nothing tells this scan how to name events yet* — and asks
   nothing of your warehouse, because there is nothing it could answer. This is
   also why **Create scan** stays disabled: a config with neither cannot ingest a
   single event, so a scan that has it would fail every run. Both controls sit
   together in the form's always-visible block; the column is picked from your
   query's columns, so load a preview first.

2. **The window is empty.** When the scan has a **Time column**, the dry run only
   reads rows inside its lookback (**Limits → Lookback (hours)**, default 24). If
   your table has no rows in that window — a staging table, a backfill that
   stopped, a timestamp column in the wrong unit — there is nothing to name. The
   summary prints the window it used; widen the lookback or clear the time column
   to check. With no time column there is no window at all: the summary says *No
   time window — the whole base query was read*, and an empty answer is then
   about your whole query, not about a window.

3. **The event name format is broken.** If the panel shows *Event name format
   error*, that is the whole answer: a format referencing a key the rows cannot
   supply fails **every** run of the config, not just the preview. The message
   names the missing key and lists the keys that are available. Fix the format
   before creating the scan — this is exactly the failure the dry run exists to
   catch early.

4. **Cardinality collapsed everything into one event.** A column with more
   distinct values than the **Cardinality threshold** becomes a `${column}`
   template rather than one event per value, so thousands of rows can legitimately
   produce one event. The panel names each collapsed column and its distinct
   count. Raise the threshold, or name the column in **Event name format** — a
   column the name is built from is always enumerated regardless of cardinality.

5. **The columns are unmapped or reserved.** With an explicit **Event type**, a
   scan only uses columns that event type already declares; the rest are listed
   as **unmapped** and are skipped by a real run too. Columns filling a reserved
   role (event type, time, app version, platform, or an event-group-rule column)
   are listed as **reserved**: tripl already uses these, so they never become
   event fields, and their absence from the plan is intentional.

6. **The derived name came out empty.** Where the **Event name format** resolves
   to nothing — every placeholder it names came out empty for those rows,
   whether from a NULL column or from a JSON path those rows do not carry — the
   row is skipped rather than turned into a nameless event, and the panel warns
   *Skipped N rows whose derived event name was empty* (singular *row* when N is
   1). A real run does the same, so this is not a preview artefact. Fix it in the
   format (name a column those rows actually populate, or add a literal segment)
   or in the base query (filter the NULL rows out, or coalesce the column). Note
   that a name with empty **segments** — `::`, `onboarding:start:` — is *not*
   this case: those are real identities and are planned normally.

If the events list is present but prefixed with **at least**, nothing is wrong —
the sample hit its cap, and the count is a floor rather than a total. That case
has its own entry below.

---

## The preview says "would create **at least** N events"

**Symptom.** The preview names events, but hedges the count.

Nothing is wrong. The dry run reads the most common column combinations inside
the scan's lookback window — or across the whole base query, if the scan has no
time column to window on — up to a cap of 5,000. When it hits that cap, more
distinct events exist than it looked at, so N is a **floor**: the scan would
create at least that many, possibly more. Saying a flat *N* there would be the
one claim the panel is built not to make.

The panel also prints *More distinct events exist than this preview looked at*,
followed by the remedies that apply to your scan. There are two, and the panel
names the second one only when your scan actually has a window to shorten:

- **Narrow the base query.** Fewer columns means fewer combinations, so the same
  cap covers more of your data. Dropping a high-cardinality column you were not
  going to name events from is usually enough.
- **Shorten the window.** **Limits → Lookback (hours)** decides how much data the
  dry run reads at all. A shorter window with the same cap is more likely to be
  complete — but remember it is then a statement about less of your data, not
  about more of it. The lookback needs a **Time column**: it is the predicate the
  window is expressed on, so the form asks for the column before it offers the
  field. Without one there is no window, the cap is the only bound, and the panel
  does not offer this remedy at all.

There is deliberately no third remedy. The sample cap itself (`sample_row_limit`)
is an API-only field with no control in the product, so the panel never suggests
raising it.

A count with no *at least* is exact **for what it read**: every distinct event in
the sample, with the exact number of sampled rows behind each one. It is still
not a table-wide total, because the lookback window is a separate bound. The
panel always says which window it used, including when there was none.

See [Scans → The dry run](feature-reference.md#the-dry-run--what-this-scan-would-create).

---

## A scan run fails / a data-source connection test fails

**Symptom.** A scan run ends in `failed`, or the **Test connection** button on a
data source returns an error.

**How errors are surfaced.** Raw driver/ORM exceptions embed hostnames, ports,
and library names, so tripl never shows them verbatim. User-facing fields get a
sanitized summary instead. A failed **scan run** reads:

- *"Scan failed: the data source did not respond in time."* — a timeout.
- *"Scan failed: could not connect to the data source."* — connection refused,
  DNS failure, network unreachable, reset, etc.
- *"Scan failed due to an internal error."*
  — anything else.

A failed **connection test** is sanitized the same way but worded for what it
is: it always begins *"Connection test failed"* and never *"Scan failed"* — a
source you have never scanned cannot report a failed scan.

- *"Connection test failed: the data source did not respond in time."*
- *"Connection test failed: the server does not offer TLS, and this connection
  requires it. Enable TLS on the server, or set SSL mode to disable for a server
  you reach over a trusted network."* — PostgreSQL only. An unset **SSL mode**
  resolves to `require` for every host except localhost, and a fresh local
  PostgreSQL (the one in a Docker Compose stack, say) has no TLS.
- *"Connection test failed: could not reach the data source — check the host,
  port, and network."*
- *"Connection test failed: authentication was rejected — check the
  credentials."*
- *"Connection test failed. Check the connection settings and try again."* —
  anything else.

The full exception (with host/port/driver detail) is only in the **worker
logs**, so always check there first:

```bash
docker compose logs --tail=200 celery-worker | grep -iE "scan failed|connection"
```

A handful of **scan** conditions are surfaced **verbatim** because they're
actionable. They all begin `Scan failed:` — that prefix is what tells the UI a
message was authored by tripl and is safe to show as-is, so the backend adds it
to every curated message rather than leaving each raise site to remember it:

- **Row limit reached.** *"Scan failed: The scan query reached the configured row
  limit (50000); increase scan_row_limit to avoid partial generation."* The default cap is
  50,000 rows for scans (100,000 for metrics). Narrow the base query, set a
  time column + lookback so less data is scanned (the **Time column** field is on
  the form in both modes — a Catalog only scan can be windowed too), or raise the
  per-config row limit. Catalog-metric collection reports the same condition as *"… reached the
  metric query row limit (100000) for chunk …"* and **fails the chunk on
  purpose**: collection replaces a window by deleting it and re-inserting, so
  writing a capped result would erase the tail of the window rather than leave
  it as it was. Narrow the metric's breakdown or replay in shorter chunks.
- **Misconfigured event typing.** *"Scan failed: This scan has no Event type and
  no Event type column, so it cannot name any events. Set one under the scan's
  Configuration tab."* The config names neither, so it cannot name a single event. The scan form no
  longer lets one be created or saved — **Event type** and **Event type column**
  are asked for together in the always-visible block, and **Create scan** stays
  disabled until one of them is answered — so a config in this state was made
  through the API or predates that gate. Open it under **Govern → Scans**, answer
  the question, and save.
- **Event name format references a column that is gone.** *"Scan failed: the
  event name format references unknown keys: `action`. Available keys: …"* The
  scan's **Event name format** names a column the query no longer supplies —
  usually because the column's field was deleted from the event type, often by
  accepting a `missing_field` schema drift. Fix it by editing the Event name
  format so it only references columns the query returns, or by re-declaring the
  field on the event type. tripl now refuses the drift accept that causes this
  (see [Schema drift](./feature-reference.md#schema-drift)). A missing JSON
  *path* no longer produces this error — a row that does not carry the key
  contributes an empty segment instead — so the message now really does mean the
  query stopped supplying a **base column**. Naming the **Event type column** in
  the format is supported as well, and no longer produces it either.
- **A replay period that reaches into the interval still filling.** *"Scan
  failed: the replay end 2026-09-12 14:00 UTC falls inside the current `6h`
  interval, which is still filling. Choose an end at or before 2026-09-12 12:00
  UTC."* This is now refused **at submit** rather than becoming a failed run: the
  replay call answers `400` before any run is created, naming the same latest
  acceptable end, and the **Replay a period…** dialog shows that message.
  A replay started from the UI or the API therefore no longer reaches the failure
  above; it survives for a run that was already queued when the instance was
  upgraded.

**Likely causes & fixes for connection failures.**

| Adapter | Common cause | Fix |
| --- | --- | --- |
| **PostgreSQL** | TLS negotiation or unreachable host. Non-local hosts default to **`sslmode=require`**, so a remote server with no TLS fails loudly rather than silently falling back to plaintext; localhost defaults to `prefer`. | Confirm host/port reachable from the worker container; check the server's TLS settings. A remote server that genuinely has no TLS needs `sslmode` set explicitly to `prefer`/`disable` on the data source. |
| **ClickHouse** | Wrong host/port/secure flag, or a probe query that returns no rows. | Verify connection params; *"Connection probe returned no rows"* means it connected but the probe was empty — check the query/permissions. |
| **BigQuery** | Missing project id or invalid service-account JSON. The probe names which of the two it is — see the verbatim messages below. | Set the project id in the host field and paste valid service-account JSON. |

**A configuration problem tripl can name is shown in full.** Those messages hold
no host, port or credential, and generalizing them away would send you to
re-check settings that are all correct, so they follow the prefix unchanged:

- *"Connection test failed: BigQuery: host (project_id) is required"*
- *"Connection test failed: BigQuery: service-account JSON credentials are
  required"*
- *"Connection test failed: BigQuery: invalid service-account JSON: …"* (with
  the position of the syntax error, never the contents of the key)
- *"Connection test failed: PostgreSQL 13.23 is too old for tripl: every
  time-bucket query uses date_bin(), which was added in PostgreSQL 14. Upgrade
  the server to 14 or newer."*

Everything else collapses to one of the four categories above, because the raw
driver text carries host, port and credential detail.

**Test connection** persists `last_test_status` and the sanitized
`last_test_message` on the data source and invalidates the cached list, so the
result you see in the UI is the actual probe — not a stale value. The button and
the background probe write that field through the same rule, so the same failure
reads the same way whichever one ran.

The scan detail shows this curated error beside the failed run. Identical recent
failures collapse into a **failed last N runs** streak; expand it when you need
the individual attempts. After fixing the source/query/configuration, use **Run
again** on the failed scan. This starts a new run and preserves the earlier
failure history.

:::note A config that keeps failing slows down on its own
Scheduled collection is due whenever a new complete bucket exists, and a run that
fails writes no bucket — so a broken config used to be retried on every dispatcher
tick (every five minutes) no matter what its interval said. After three
consecutive failures it now waits instead, roughly doubling the gap each time,
never longer than 24 hours and never shorter than the config's own interval.

Two consequences worth knowing: a fixed config can take up to that wait before it
retries by itself — press **Run again** if you don't want to wait — and the
backoff only counts *scheduled* runs, so your manual runs neither trigger it nor
clear it.

The same floor applies to two quieter versions of the loop. A run that
**succeeds but collects nothing** — an empty warehouse window, or events that
match nothing — writes no bucket either; it now counts as having covered that
window, so the config is tried again at its own interval instead of every five
minutes. And a **catalog metric** whose last collection errored waits one
interval before the scheduler retries it (an hour for an event-composition
metric, which has no interval of its own). **Collect now** on the metric ignores
the wait, and it reaches back to wherever the metric actually left off rather
than only over the recent window — the buckets it missed while it was failing
are collected, not skipped.

That wait is measured from the failure itself, so **editing the metric does not
restart it** — you can fix the SQL, save, and still be collected at the moment
you would have been anyway. The metric's own page shows when: while it is
waiting, **Next collection** is that moment rather than "due now", which is the
same answer the scheduler is working from.
:::

:::note
Scan tasks have a hard time limit of 60 minutes (the worker's default) and do
**not** retry automatically (`max_retries=0`). Metrics collection gets a much
longer hard limit (25 hours) so a long historical replay isn't killed mid-run —
but it also doesn't retry. A genuinely slow warehouse query is killed at the
limit rather than retried; shrink the window or row count instead of waiting it
out.
:::

---

## A branch merge is blocked

**Symptom.** Merging a plan branch returns an error instead of merging.

**Likely causes** (each maps to a specific API error):

| Error | Meaning | Fix |
| --- | --- | --- |
| `400 Branch is already merged` | The branch was merged previously. | Nothing to do; open a new branch for further changes. |
| `409 Branch must be approved before merging` | The branch isn't in the approved state. | Get the required approvals first. |
| `409 incomplete_base_snapshot` | The branch was created before complete merge baselines were available, so a safe three-way merge is impossible — and **Update from main** cannot help either. | Copy the intended changes to a new branch opened from current main. |
| `409 conflicts` | Both sides changed the same state differently, or one side deleted a parent while the other added/changed a child. | Click **Update from main** on the branch, choose **Keep this branch** or **Take main** for each overlap, then merge again. |
| `409 unresolved_field_conflicts` | An event-type **field** was changed on both branch and main relative to the base. | Resolve each field in the Conflicts panel (**Take main** or **Keep this branch**), then merge again. |
| `409 missing_owner_approvals` | The branch touches an **owned** event type without that owner's approval. | Request approval from the listed owner(s) before merging. |
| Update from main: "This update cannot run yet" (`409 update_blocked`) | Either a row main added or renamed and a row this branch added or renamed would end up with the same name or source name, or main changed an event or relation this branch holds twice under one name (a branch opened before origin tracking). | For a clash, rename the branch's own row and update again. For a namesake, copy the intended changes to a new branch opened from current main. |

**Fix.** Field-level (modify/modify) conflicts on event types are resolvable
through the inline resolution flow. Every other conflict — other entity kinds,
and one side deleting what the other edited — is resolved by **Update from
main**: it brings main's changes into the branch, you choose **Keep this
branch** or **Take main** for each overlap, and the next merge has nothing left
to refuse.

---

## Migration or startup failure

**Symptom.** The `app` (or `migrate`) container exits on boot, or the API
refuses to start.

### Production startup checks failed

In non-debug (production) mode the API runs `assert_production_ready()` during
startup and **refuses to boot** with `RuntimeError: Production startup checks
failed:` followed by a bulleted list. Each bullet is a missing/unsafe setting:

- **`ENCRYPTION_KEY` is empty or not a valid Fernet key.** Data-source and
  alert secrets would be stored as plaintext. Generate one:

  ```bash
  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  ```

- **`SECRET_KEY` is empty.** Session token hashes would be unkeyed. Generate:

  ```bash
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
  ```

- **`SESSION_COOKIE_SECURE=false` in production.** Set it `true` when serving
  over HTTPS (the production compose stack already does).
- **CORS resolves to empty or to the wildcard `*`.** Set `CORS_ALLOW_ORIGINS`
  or `APP_BASE_URL` to your explicit frontend origin. A wildcard breaks
  cookie-based auth because browsers reject credentialed requests against `*`.
- **A connection URL still uses the dev defaults** (`tripl:tripl` or
  `guest:guest`). Set real credentials for `DATABASE_URL`, `SYNC_DATABASE_URL`,
  and `RABBITMQ_URL`.

The production compose file wires these from `.env` and will refuse to even
render if `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD`, `ENCRYPTION_KEY`,
`SECRET_KEY`, or `APP_BASE_URL` are unset. See
[Configuration](../run/configuration) and [Deployment](../run/deployment).

### Schema migration failed

In production, schema upgrades run **once** via the `migrate` one-shot
(`alembic upgrade head`) *before* the app and workers start — `app`,
`celery-worker`, and `celery-beat` all wait for
`migrate: service_completed_successfully`. If `migrate` fails, the app never
starts. Inspect it:

```bash
docker compose logs migrate
```

Common causes: the database isn't reachable yet (`migrate` waits for
`postgres` to be healthy), or a migration can't apply against the existing
schema. Fix the underlying DB/connection issue and re-run
`docker compose up -d` — the one-shot retries the upgrade.

:::warning
Running multiple app/worker replicas never races the upgrade because the
one-shot `migrate` gate runs first. Don't add `alembic upgrade` to the app or
worker start command in production — that reintroduces the race the one-shot
exists to prevent.
:::

For local development the `api` service runs `alembic upgrade head` itself before
starting uvicorn. If a script shebang is broken after a directory rename, call
the module directly: `uv run python -m alembic upgrade head`.

---

## RabbitMQ or PostgreSQL connection errors

**Symptom.** The worker/beat logs show repeated broker connection errors, tasks
never run, or the API logs database connection errors.

### RabbitMQ (the Celery broker)

- The broker URL comes from `RABBITMQ_URL` (e.g.
  `amqp://tripl:<password>@rabbitmq:5672//`). In production it must **not** use
  the `guest:guest` dev default — the startup check rejects it.
- Celery is configured to **retry the broker connection on startup**
  (`broker_connection_retry_on_startup = True`), so a worker that boots before
  RabbitMQ is ready keeps trying rather than crashing. Persistent failures mean
  the broker is genuinely unreachable or the credentials are wrong.
- In compose, `celery-worker` and `celery-beat` wait for
  `rabbitmq: service_healthy` (a `rabbitmq-diagnostics ping` health check). If
  RabbitMQ never becomes healthy, those services won't start.

```bash
docker compose ps rabbitmq
docker compose logs --tail=80 rabbitmq
docker compose logs --tail=80 celery-worker | grep -i amqp
```

:::note
The broker's `consumer_timeout` in
[`infra/rabbitmq/rabbitmq.conf`](https://github.com/tripl-io/tripl/blob/main/infra/rabbitmq/rabbitmq.conf)
is raised to 26 hours — above the `collect_metrics` hard time limit (25 hours) —
so a long metrics replay that holds its delivery unacked for the whole run isn't
force-requeued mid-run. If you replace that config, keep the consumer timeout
above the `collect_metrics` time limit or long replays will be redelivered and
run twice as duplicate, competing executions.
:::

### PostgreSQL (application database)

- The async API uses `DATABASE_URL`
  (`postgresql+asyncpg://...`); Celery workers use `SYNC_DATABASE_URL`
  (`postgresql+psycopg://...`). **Both** must point at the same database with
  real credentials. A worker that can't reach Postgres can't claim jobs or write
  results, even if the API is fine.
- The connection pools use pre-ping, so a connection dropped by the DB
  (restart, idle timeout) is detected and replaced transparently — you don't
  normally need to restart workers after a brief Postgres blip.
- In compose, services wait for `postgres: service_healthy`
  (`pg_isready -U tripl`).

```bash
docker compose ps postgres
docker compose logs --tail=80 postgres
docker compose exec postgres pg_isready -U tripl
```

If the API starts but the worker errors, double-check that **both** URLs are
set and use the right driver prefix (`+asyncpg` for the API, `+psycopg` for the
worker).

:::tip
Redis is optional. `REDIS_URL` being empty disables caching (every read falls
through to the database) but does not break anything — so Redis connection
problems degrade performance, they don't stop scans, metrics, or alerts.
:::

---

## FAQ

**An old link or bookmark points at `/p/<slug>/settings/scans` — is it broken?**
No. Scans moved to `/p/<slug>/scans` (Govern › Scans is a top-level surface, not
a settings tab). Both `/p/<slug>/settings/scans` and
`/p/<slug>/settings/scans/<scan-id>` redirect to the new paths, so bookmarks,
older docs, and the deep links in already-delivered alerts keep working.

**An alert message or bookmark points at `/p/<slug>/settings/alerting/…` (or `settings/event-types`, `settings/variables`, `settings/branches`, …) — is it broken?**
No. The Plan, Observe and Govern pages have their own addresses now:
`/p/<slug>/event-types`, `/meta-fields`, `/variables`, `/relations`,
`/branches`, `/history`, `/alerting` and `/audit`. Every old
`/p/<slug>/settings/<page>` address — with its item id, `?item=` / `?incident=`
anchors and any other query string — redirects to the new one, so alerts sent
before the move still open the exact delivery and incident. Meta fields,
Relations, History and Audit have no per-item page, so an item id after them is
dropped and the page itself opens.
`/p/<slug>/settings` itself holds only project settings: General, Plan rules and
Detection settings (`/p/<slug>/settings/monitoring`).

**How do I read a scan run?**
Open the scan, expand the run, and read **What this run did** — plain sentences
about your data, not internal counters. It tells you how many warehouse rows the
run read, which events it added to your plan, how many were already there (and
that they were left alone, not lost), how many metric points it recorded, and
whether anything it produced raised a signal or queued an alert. A catalog-only
scan says outright that it collects no metric points, so nothing downstream can
fire.

Every raw counter the run reported is still there under **Show raw counters**.
Four are always shown — *Events created*, *Properties created*, *Events skipped*,
*Columns analyzed*. The rest appear only when that run produced them, so the
panel is shorter for a catalog-only scan than for a scheduled collection:
*Event breakdowns*, *Distribution rows*, *Paths sampled*, *Paths with samples*,
*Values written*, *Contexts unfilled*, *Properties retired*, *Signals added*,
*Alerts queued*. Nothing was removed; it is one click further down. The four
sampling counters in that second group are the ones the empty-observed-values
answer below sends you to, and *Properties retired* read against *Properties
created* is how you tell a growing catalog from one holding steady — it reads
`0` rather than going missing on a run that swept and found nothing, and is
absent only on a replay, which sweeps nothing.

**The run says it raised 2 signals but Anomalies shows a different number. Which
is wrong?**
Neither. *Raised N anomaly signals* is that run's **delta** — what this run
added. The **Anomalies** page counts what is **open now**: signals from earlier
runs that have not closed, and — unfiltered — signals from other scans and
catalog-metric signals that belong to the project rather than to any scan. Two
different questions, two legitimately different answers. Where the two disagree
the run report prints the scan's current count under the sentence (*5 signals
from this scan are open now*), so you are not left comparing a number here
against a number on another page. The activity feed's "N new signals" on a scan
card is the same delta.

**Two runs both say "Scanned" but the numbers look unrelated.**
Because they count different populations. A catalog run reports the distinct
column combinations the warehouse grouped for it ("153 combos"), not warehouse
rows, bounded by **Row cap per run**; a metrics run reports the warehouse rows
read across every metrics chunk, bounded by **Row cap per metrics run**. Hover
the figure — the stat card and every cell in the run table carry a title naming
the population and its cap.

**Do I need to run scans on a schedule to get metrics?**
**Yes** — the schedule is what makes a scan a monitoring scan. A scan with no
schedule is **Catalog only**: it fills your tracking plan when you run it and
records no metric points, so nothing downstream of them can fire. Metric points
come from `collect_metrics`, and the dispatcher only ever selects scans that set
**both** a schedule and a time column — the pair **Catalog + monitoring** asks
for. What you never have to do is trigger that collection: pick the mode, and
`celery-beat` dispatches it from then on. Starting a run by hand adds events and
fields to your plan and writes no metric point — and the one manual metrics path,
**Replay a period…** in the scan page header, is itself disabled until the scan
has both.

**Why is my brand-new scan not flagging any anomalies?**
Anomaly detection needs history. Until enough buckets accumulate, the detector
uses a rolling fallback and skips low-volume series; with very little data it
will correctly report nothing. Give it time, and check the project's anomaly
settings (sigma threshold, minimum expected count, baseline window).

To look at just this scan instead of the whole project, open the scan, expand a
run, and click **Signals added** — it opens Anomalies filtered to that scan
(`/p/<slug>/anomalies?scan=<scan-config-id>`). On a busy project one large scan
can supply most of the page, so per-scan is often the only readable view. If the
counter reads `0` it is not a link: that run raised nothing, which is itself the
answer. If the link opens on *No open anomalies from &lt;scan&gt;*, the signals
that run raised have closed since — the scan stays selected so you can see that
is what happened, and **Show all scans** widens the view.

Also check the scan's mode. **Catalog only** scans record no metric points, so
they raise no anomalies by design — the scan's own page says so in one line under
its name, and its row carries a **Catalog only** badge. A scan badged **Needs a
time column** has a schedule but no time column, so the scheduler never runs it
and it collects no metric points — runs you start by hand still add events to
your plan, which is why that scan can have green runs and no anomalies at the
same time.

**A scan failed with a generic "internal error" — where's the real reason?**
User-facing fields are sanitized to avoid leaking host/port/driver details. The
full exception is in the worker logs:
`docker compose logs celery-worker`.

**My alert never arrived but the UI shows an anomaly. What now?**
Walk the delivery chain in [Alerts never fire](#alerts-never-fire): destination
enabled? rule enabled and matching? cooldown/mute? (email) SMTP set? Then check
the worker logs for `Failed to send alert delivery` — the failure reason is
persisted on the delivery. If that reason is a transient network error, the
maintenance reaper retries the delivery on its own a few times, minutes apart;
any other failure — and every Jira/Linear delivery — waits for **Retry** in
the UI.

**I acknowledged an incident and wrote down why, and it alerted again. Do I need
to mute it?**
Probably not. Acknowledge really does stop deliveries, but only for as long as
that incident lives — once the scope goes quiet the row reopens itself, so the
next occurrence is a new incident and alerts. Mute is for "do not tell me before
*T*" and is the only decision that survives the incident ending; **false
positive** is for "the detector is wrong about this scope" and permanently
tightens its thresholds. See
[An incident I acknowledged fired again](#an-incident-i-acknowledged-fired-again).

**The app won't start after I set `DEBUG` off.**
That's the production readiness gate. Read the `Production startup checks
failed` bullet list in the logs and set each missing secret/origin. See
[Migration or startup failure](#migration-or-startup-failure).

**Can I retry a failed scan automatically?**
No. Scan, metrics, and connection-test tasks use `max_retries=0` — a failure is
final for that run. Fix the underlying cause (connection, row limit, query) and
click **Run again**. *Alert deliveries* are the exception: stranded ones are
re-enqueued automatically by the maintenance reaper, and one that failed on a
transient network error is retried the same way a few times, minutes apart.
Other delivery failures — and every Jira/Linear delivery — wait for the manual
**Retry**.

**Why did a deleted property come back after the next scan?**
The scan rediscovered its warehouse column or JSON-path binding. Delete removes
the plan row, while **Exclude from scans** keeps a tombstone that prevents
recreation and stops new contexts/drift. Open **Plan → Properties**, exclude the
property, and use **Restore** if the decision changes later. Automatic
retirement (below) behaves the same way — it deletes the row, it does not leave
a tombstone — so a retired property reappears if its source path starts
arriving again. Exclusion is what makes a removal stick.

**A scan-created property vanished from the plan. Where did it go?**
A catalog run retired it, so a catalog stops growing a permanent row per key of
a JSON column keyed by free text. A scan you started always retires. A scheduled
monitoring collection retires on every run too, but what it judges depends on
the property: one minted from a path inside a JSON column is judged on every
run, while one minted from a scalar column is judged only when the scan sets
**Limits → Lookback (hours)** — with the field blank the run reads the slice it
is collecting, often a single hour, and a scalar column that looks enumerable
for one quiet hour is rewritten as literals in every event at once, which is not
evidence its property is dead. A metrics **replay** syncs no catalog and retires
nothing. A row is removed only when all of this holds: its description and
display name are still the scan's own, its bindings are still only the source
path the scan gave it, it documents no values, and it has no per-event override,
no value drift, no observed context, and no stored event field or meta value
naming any of its tokens as `${token}`. Anything you renamed, edited,
documented, overrode, triaged, or excluded is kept — and so is a property that a single `${token}`
still names, even with no observed contexts. The run's details list reports the
count, and **Plan → Properties** has an **Unused** filter that shows exactly what
a run would take. See
[Properties & templates](./variables-and-templates.md#unreferenced-scan-created-properties-are-retired-automatically).

**A property's observed values read "No values stored". What is it telling me?**
That the property has contexts — some event field does refer to it — and not one
of them holds a value. It is a different state from the dash on neighbouring
rows, which means no context exists at all. Open the event's value popover to see
which contexts are empty and what each one binds to. An empty context is not by
itself a fault: a binding pointed at a column that is genuinely empty has nothing
to store. A JSON-path context that is merely new fills on its own — scheduled
runs attempt every path still waiting for a first value every few runs, so
expect first samples within hours on a regularly collecting scan. A context
still empty after days usually means the path is not arriving — but rule out
two other causes first: a property excluded from scans is never sampled at
all, and a sampling query that fails (a permission change, a dropped column)
degrades silently so the run still completes — the run details' raw counters
show it as paths sampled with none coming back with samples. Once stored,
samples accumulate across runs, so a value does not drop off the list because
recent scan windows stopped carrying it.
Note also that a property with no stored values raises no value drift,
so an empty drift count says nothing about whether the documented contract holds.
See
[Properties & templates](./variables-and-templates.md#documented-observed-and-effective-values).

**Why does a property show value drift?**
The scan observed values outside the effective documented list (the event
override when present, otherwise the global list). Review it from Properties or
the event detail: accept globally, accept for that event, snooze, or mark false
positive. See [Properties & templates](./variables-and-templates.md).

**Why did a value drift I already accepted come back?**
Because the scan saw a value that was *not* in the set you accepted. An
acceptance covers exactly the values it documented; anything newer reopens the
row, and the row then lists only the new values. Use the **Show N …** toggle in
either review panel to inspect or reopen a drift you resolved earlier — it reads
**Show N resolved**, **Show N snoozed**, or **Show N snoozed or resolved**,
depending on what the collapsed group is holding at the time.

**Where do I configure SMTP, encryption keys, and connection URLs?**
All via environment variables / `.env`. See [Configuration](../run/configuration)
for the full list and [Deployment](../run/deployment) for the compose stack.
The authoritative defaults live in
[`backend/src/tripl/config.py`](https://github.com/tripl-io/tripl/blob/main/backend/src/tripl/config.py).

**How do I run the database migration by hand?**
In the running stack: `docker compose run --rm migrate`. Locally in the backend:
`uv run alembic upgrade head` (or `uv run python -m alembic upgrade head` if the
console script shebang is broken).

---

## Still stuck?

Collect the relevant logs and open an issue on
[GitHub](https://github.com/tripl-io/tripl/issues). Useful context to include:

```bash
docker compose ps
docker compose logs --tail=300 celery-worker
docker compose logs --tail=100 app
docker compose logs --tail=100 migrate
```

Redact any secrets before sharing. tripl already keeps host/port/credential
detail out of user-facing fields, but raw logs may contain connection strings.
