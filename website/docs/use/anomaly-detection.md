---
title: How anomaly detection works
sidebar_position: 5
---

# How anomaly detection works

This page explains *why* a particular time bucket gets flagged as anomalous, and how to make the detector more or less sensitive. It is written for analysts and admins who want to understand and tune the behaviour — not for people changing the code.

## If you only read one section

tripl watches each event's own history and learns its rhythm — busy at lunchtime, quiet at 3am, slower on Sundays. When a period comes in far enough outside what *that* event normally does at *that* time of week, it raises a signal.

Three things follow from that, and they explain most of what people find surprising:

- **A predictable dip is not an anomaly.** Every night's trough is compared against other nights' troughs, not against the afternoon peak. That is deliberate, and it is why you are not paged every evening.
- **New or very quiet events flag less.** There has to be enough history to know what normal looks like, and on a low-volume event a swing of a few counts is genuinely just noise.
- **Signals arrive a little after the fact.** The newest period is charted immediately but not *judged* until your warehouse has had time to finish delivering rows for it — otherwise every scan would flag a half-full bucket as a drop.

If a signal looks wrong to you, the usual fix is not "turn detection off" but one of the sensitivity dials in **Project settings → Monitoring**, described further down. The rest of this page is the *why* behind them.

## The pipeline in one paragraph

Every scan reads your warehouse and rolls the raw events up into **time-bucketed counts** — one count per event (and per event type, and for the project as a whole) for each time bucket (15 minutes, hourly, 6-hourly, or daily, depending on the scan's interval). The detector then compares the **most recent bucket(s)** against a **baseline** built from the history of that same series. For each bucket it produces three numbers: an **expected** value (where the baseline thought the count should land), a **spread** (how much that series normally wobbles), and a **z-score** that says how many "normal wobbles" away from expected the actual count fell. When the z-score is large enough and the expected count is high enough, the bucket is recorded as a detected anomaly with a direction of **spike** (too high) or **drop** (too low). That record is the raw material every alert rule later consumes.

The very newest buckets are the one exception: they are collected and charted immediately, but held back from *scoring* until your warehouse has had time to finish delivering rows for them. That is the **ingestion-settling allowance**, and it is why a signal shows up somewhat after the bucket it describes — see [Detection latency](#detection-latency).

:::note
Anomaly detection is **off by default**. Nothing is flagged until an admin enables it in **Project settings → Monitoring** (where all the sensitivity controls below also live). See [Alerting](./alerting.md) for turning detected anomalies into notifications.
:::

## Baselines: what "expected" is measured against

The hard part of anomaly detection is deciding what *normal* looks like for a series that breathes with the time of day and the day of the week. Traffic naturally dips at 3am and on weekends; a naive "average of the last N buckets" baseline would flag every nightly trough as a drop. The detector avoids this with a **phase (seasonal) baseline**, and falls back to a simpler **rolling baseline** only when there isn't enough history yet.

### The phase (seasonal) baseline — the primary signal

The phase baseline compares each bucket only against **past buckets at the same phase** in the seasonal cycle. For an hourly series, "same phase" first means *same hour-of-week* (the same hour on the same weekday across prior weeks); if there isn't enough history for that, it relaxes to *same hour-of-day* (the same hour across prior days). A Monday-9am bucket is therefore judged against previous Monday-9am buckets, not against last night's quiet hours. This is what kills the recurring-trough and recurring-peak false positives: a predictable dip is compared against other predictable dips and scores near zero.

The "expected" value here is the **median** of those same-phase historical counts, and the spread is a **robust** measure of how much they scatter (described below). Medians and robust spread are used deliberately: a single past outlier (an earlier spike or outage) barely moves the median, so one bad day in history doesn't poison the baseline for weeks.

A phase period only becomes usable once there are at least **3 complete cycles** of same-phase history before the bucket being judged. Until then, that bucket uses the rolling fallback instead.

The phase baseline is **level-adaptive**. Each same-phase historical count is divided by the average level of its own cycle, so the seasonal *shape* is measured independently of the overall *level*, and the expectation is then re-scaled to the **current** level. This is what keeps a sustained level change from flagging every bucket: an event whose volume steps up several-fold and then holds steady is reported **once** by the trend-shift detector (below) instead of tripping every hour for a week or two while a plain same-phase median slowly catches up. A genuine one-bucket spike still stands out, because the current level (measured over a trailing full cycle) barely moves for a single outlier.

On low-volume series the phase baseline also applies a **Poisson (√N) spread floor**: a scope expecting ~11 events per bucket has a natural ±√11 wobble, so a +3 count move is noise, not a spike. Without this floor a quiet event could flag almost every bucket. The trade-off is that small absolute changes on low-volume scopes (say 12 → 3) no longer flag; high-volume scopes are unaffected because √N sits far below their real spread.

### The rolling baseline — fallback for new or sparse series

When a series is too young to have three full seasonal cycles (a brand-new scan, or a very sparse event), the detector falls back to a **seasonality-blind rolling baseline**: the plain **mean** and **standard deviation** of the most recent window of buckets (the window length is the `baseline_window_buckets` setting, **Baseline window (buckets)** in the UI). This baseline knows nothing about hour-of-week, so it is less precise on cyclic data — but it lets monitoring produce *something* on day one instead of staying silent. The rolling baseline also refuses to fire until it has seen at least `min_history_buckets` real buckets in its window (**Min history (buckets)** in the UI).

Both are counted in **buckets**, not hours, and the settings page says so under them: *a bucket is one collection interval of the series being scored*. So 14 buckets of baseline is 14 hours on an hourly scan and 14 days on a daily catalog metric.

**Min history (buckets)** cannot exceed **Baseline window (buckets)**: a longer minimum would prevent the rolling baseline from ever scoring. When updating monitoring settings through the API, an explicit `null` leaves that setting unchanged; send a concrete value to change it.

### Seasonal decomposition and the trend-shift detector

On top of the per-bucket phase check, the detector runs a second, slower-moving test built on **seasonal decomposition** (STL for a single season, MSTL when both a daily and a weekly season are present). Decomposition splits the series into three layers: a smooth **trend**, the repeating **seasonal** shape, and the left-over **residual** noise.

The **trend-shift detector** works entirely on the deseasonalized trend layer. It compares the trend level *now* against the trend level *exactly one full seasonal cycle ago*, scaled by the robust spread of the residuals. Because it operates on the deseasonalized trend, it can never be fooled by the time of day — its job is to catch **slow, sustained level changes** (a gradual 30% decline over a week) that the per-bucket band quietly absorbs one bucket at a time. A shift must clear both the sigma threshold and a **15% relative effect-size gate**, and a contiguous run is collapsed into one anomaly instead of one row per bucket, dated at the first bucket whose own value actually left its expected level — not the earlier hour where the smoothed trend first starts to bend. If that one row cannot be written (for instance its expectation is under `min_expected_count`), the run's per-bucket anomalies are kept rather than silently dropped. A shift is also never reported on a bucket that has neither an expectation nor any traffic: with nothing expected and nothing observed there is no movement to describe. Only that pair is excluded — an empty bucket against a real expectation is still a drop, and traffic against a zero expectation is still a spike. The same decomposition also powers the forecast band you see drawn ahead of the latest data in the UI.

When both detectors flag the same bucket, the one with the **larger absolute z-score** wins, so you see the more significant explanation.

### An event that goes silent is reported once

A scope that stops emitting keeps scoring the same way for as long as it stays silent, so without a rule against it one outage produces a fresh row every bucket of every scan — on an hourly scan, dozens per day for as long as the event is dead. Instead, a contiguous run of empty buckets is reported **once**, at the bucket where the scope stopped behaving normally, and stays quiet until it emits again. If it revives and dies a second time, that is a second run and a second report.

"Stopped behaving normally" is not the same as "first empty bucket", and the difference matters for any event that is *legitimately* quiet part of the time — business hours, one region's traffic, anything with a nightly trough. For those, the run of empty buckets begins with a perfectly normal zero that was never anomalous; the report is anchored on the first bucket in the run that actually was.

The marker can land a few buckets **after** the moment the traffic stopped, and that is expected rather than a rounding error. The anchor decides *which* run gets reported, but the report itself has to sit on a bucket the detector was allowed to flag — and `min_expected_count` blocks any bucket whose phase normally expects fewer events than that. An event that trickles through the small hours and only gets busy at 06:00 therefore dies at 02:00 but is marked at the first busy hour after it, because the 02:00 phase is below the volume floor you set. It is still one report for the whole outage, and it stays on record from then on: later scans re-derive the same anchor, see it behind their window, and leave the existing marker alone instead of rewriting it.

Reported once does **not** mean visible for a day. That single report stays an **open signal** for as long as the scope is still silent, however old its bucket gets — a five-day-old outage is still listed, still counted on the badge, and still in the Overview **Open signals** stat. tripl re-checks it against the stored series rather than against the report's own age: the report says the scope was at zero **and that it normally has volume** — it had something to lose — the series says it has produced nothing since, and the scan says it is still collecting for other scopes. The moment the scope emits again the signal closes on its own. Nothing new is emitted to keep it open, so an outage still costs exactly one row.

**Alerting judges it the same way.** A rule watching that scope keeps its state **open** for as long as the outage is running, rather than resolving itself after a day because the one report describing it got old. That is what keeps the monitor's status honest — it cannot read *healthy* while the Anomalies page still lists the scope as down — and it stops an incident you have already acknowledged in the Inbox from quietly un-acknowledging itself while it is still going. Holding the state open costs no repeat messages: the report's bucket never advances, so there is nothing new to notify you about (see the note on `recent_signal_window_hours` below).

Clicking that row opens the scope's chart, and the chart follows the report rather than the other way round. The detail page opens on the last 7 days, so an outage that started before then would otherwise have no row of any kind inside the range it loads — the page for the incident you just clicked would show nothing at all. When the scope's open report predates the selected range, the range is extended back far enough to include it. Only a report that is **still open** does that; an older one that has since closed leaves your selection exactly where you put it.

Three things close it while the scope is still down — on the page and in alerting alike. This holds for scopes backed by a scan; a **catalog metric** has no scan whose health could vouch for it, so its outage report is judged on its own age instead and drops off once that age passes the freshness window. The first is the scan itself going quiet: if *nothing* on that scan has collected within the freshness window, a switched-off collector and a dead event look identical from the stored data, and tripl will not leave a scan red on that basis. (A drop to zero on the **project total** is that case by definition, so a whole-scan blackout is judged on scan health rather than held open here.) The second is a **zero baseline**: a report whose expected count is itself zero describes a scope that was not expected to emit anything, so there is no incident to hold open and it is judged on its own age like any other signal. The third is retention — an anomaly record is eventually trimmed like any other, and a scope nobody ever brought back stops being news long before then.

:::note Why a zero-versus-zero report cannot be an outage
A scope holding an outage open is checked against the series it stopped producing, and a scope that never produced anything stores no rows to check — its head stays frozen at the report, so the "still collecting" test stays true for it forever. Reports of that shape (`0 actual` against `0 expected`) were therefore permanently open, and were the one shape nothing could ever close. The detector no longer writes them at all — a bucket with neither an expectation nor any traffic is not a level shift — so the rule above now applies only to reports already on record, including those a project running `min_expected_count = 0` accumulated before the change. What that changes is the **denominator** on the Anomalies page: the *N* of "M of N open" no longer counts rows nobody could act on. The **Significant** view, the sidebar badge and the Overview headline do not move, because a zero-versus-zero row has no magnitude and never passed the gate those surfaces apply.
:::

This applies to volume series only — and to *every* volume series, not just the scope total. A single **breakdown slice** that dies (one platform, one country, while the rest of the event carries on) is reported once and keeps that report exactly the same way, so a value that disappears from the Breakdowns tab leaves a marker that stays put rather than one that vanishes on the next scan. **App-version slices are the exception** and carry no volume markers at all: a version's share rises and falls with its rollout, so scoring it against its own past would flag every release twice — once as it ramps up and once as it retires. Releases are compared against each other instead, by the release-regression check described below. Anything measured as a fraction is outside this rule: for a catalog metric with a ratio or average value, and for the platform-share comparison below, zero is a value like any other rather than an absence.

## The score: how a bucket is flagged

For every evaluated bucket the detector computes:

- **expected** = the baseline centre (median for the phase baseline, mean for the rolling one).
- **spread** = a *robust scale*: the **median absolute deviation** (the median of how far each historical point sits from the centre) multiplied by **1.4826**, which rescales it to be comparable to a standard deviation on normal-looking data. If every historical point is identical (median absolute deviation of zero), it falls back to the ordinary standard deviation.
- **z-score** = `z = (actual − expected) / spread`.

A bucket is flagged only when **both** guards pass:

1. `|z| ≥ sigma_threshold` — the deviation is large relative to the series' normal wobble, and
2. `expected ≥ min_expected_count` — the baseline volume is high enough to be worth judging.

The **sign of z** sets the direction: positive z is a **spike**, negative z is a **drop**.

### The spread floor (why a flat series can't blow up)

There is one more guard hidden inside the spread. A perfectly stable series has almost no scatter, so its raw spread approaches zero — and dividing by something near zero turns any tiny wobble into a giant z-score. To prevent that, the spread is clamped from below to a **relative floor**: it is never allowed to be smaller than the larger of `1.0` and a small fraction of the expected count. The fraction differs by detector, because each one has a different natural noise level:

| Baseline | Relative floor on the spread |
|---|---|
| Rolling baseline (counts) | larger of 1, ~3% of expected, and `√expected` |
| Per-bucket phase baseline | ~5% of expected (a single point per phase is noisier) |
| Trend-shift detector | ~5% of expected, plus the separate 15% effect-size gate |
| Fractional metric | ~4% of the series' robust magnitude (with a tiny epsilon floor) |

In plain terms: on a series running around 1,000 events, a phase baseline won't treat anything inside roughly ±50 (5%) as remarkable, no matter how flat the history looks. The floor anchors the z-scale to "a *noticeable change relative to volume*" instead of to raw counts.

### Why each guard exists

- **The minimum-count gate (`min_expected_count`)** silences low-traffic noise. On a series expecting 4 events, a jump to 12 is a 3× swing but statistically meaningless — small counts are dominated by randomness. Requiring a minimum expected volume keeps the detector from screaming about every sparse event.
- **The spread floor** kills divide-by-tiny blow-ups on near-constant series, as described above.
- **The Poisson floor** on the count-shaped rolling fallback acknowledges that a
  count process around `N` naturally wobbles by roughly `√N`; it prevents small
  low-volume jitter from looking multi-sigma merely because history was flat.
- **The trend effect-size gate** requires a visible level shift, not just a
  statistically tidy one. Smooth fractional ramps of at least four steps are
  deferred from per-bucket detection to this trend path so they surface once.
- **The sigma threshold** is the headline sensitivity dial: it sets how many "normal wobbles" of deviation are required before anything is flagged. Every chart draws its confidence band with the same multiplier the detector used for that scope — the project setting, or the scope's own tightened value once the false-positive ratchet has raised it — so "outside the band" always means "flagged". That includes catalog metrics.

### The baseline band on the chart

The volume chart of an event, an event type and the project total draws the expected value and its normal range (`expected ± sigma × effective stddev`) on **every bucket the detector scored**, not only on flagged ones: each scan stores the baseline it judged each bucket against, flagged or not. A bucket carries no band when the detector did not score it — too little history yet, an expectation under `min_expected_count`, a bucket still inside the ingestion settling allowance, or a series too quiet to ever clear the volume floor. A flagged bucket shows the expectation of the anomaly itself, which for a trend shift is the pre-shift level rather than the per-bucket one. A reported level shift or outage is one row for the whole run, not one per bucket, so the other buckets inside it that left their band show **no band** rather than an unflagged point outside it: the incident is marked once, on the bucket that carries its row, and "outside the band" still always means "flagged".

Baselines are stored from the release that introduced them onward and are not backfilled: history scored before that shows the band on flagged buckets only. A scheduled scan re-scores only its own trailing evaluation window, so from then on only the buckets inside each scan's evaluation window gain a band, plus any older range you re-score with **Replay a period…**; history older than that keeps flagged-only bands. Catalog metric charts and breakdown series still draw the band on flagged buckets only.

### A worked example

Suppose an hourly event type's Monday-9am buckets over the last several weeks were:

```
940, 980, 1000, 1000, 1040, 1060
```

The median of those is exactly **1000**, so **expected = 1000**. The robust spread works out to roughly **45** (the typical distance from the centre, scaled by 1.4826). The 5% phase floor is `0.05 × 1000 = 50`, which is larger than 45, so the **effective spread is 50**.

This Monday at 9am the count comes in at **1240**:

```
z = (1240 − 1000) / 50 = +4.8
```

With the default `sigma_threshold = 4.0`, `|4.8| ≥ 4.0` passes, and `expected = 1000 ≥ min_expected_count` passes, so the bucket is flagged as a **spike** (positive z) with expected 1000, spread 50, and z = 4.8. Had the same series instead risen only to 1180, that would be `z = (1180 − 1000) / 50 = +3.6`, which is **below** the threshold and would **not** fire. Raising sensitivity (a lower sigma) would catch that 1180; lowering it (a higher sigma) would let through only larger swings.

## Tunables and defaults

These live in the project's **Detection settings** (route `/p/<slug>/settings/monitoring`) and apply to every scan in the project. (The two most impactful values, `sigma_threshold` and `min_expected_count`, can also be raised automatically for a **single scope** — see [False positives](#false-positives-self-tune-the-thresholds) below.)

On the page the dials are grouped into **Sensitivity** (`sigma_threshold`,
`min_expected_count` and the fallback baseline's window and history) and
**Timing** (`recent_signal_window_hours`, `anomaly_ingestion_settling_minutes`),
each group with a **Learn more** link to this page. A value outside its allowed
range stays in the box, marked **Not saved**, until it is corrected; nothing
out of range is written. Turning `anomaly_detection_enabled` off asks for
confirmation first, and while detection is off a banner says so on Detection
settings and on **Alerting › Rules**, so alert rules that can no longer fire do
not look healthy.

| Setting | Default | What it does |
|---|---|---|
| `anomaly_detection_enabled` | `false` | Master switch. Nothing is detected until this is on. |
| `detect_project_total` | `true` | Watch the project-wide total volume. |
| `detect_event_types` | `true` | Watch each event type's volume. |
| `detect_events` | `true` | Watch each individual event's volume. |
| `detect_metrics` | `true` | Watch each active metric (the metrics catalog). |
| `baseline_window_buckets` | `14` | How many recent buckets the rolling fallback baseline averages over. Labelled **Baseline window (buckets)** in the UI. |
| `min_history_buckets` | `7` | Minimum buckets the rolling fallback needs before it will fire. Labelled **Min history (buckets)** in the UI. |
| `sigma_threshold` | `4.0` | How many normal wobbles of deviation are required to flag a bucket. |
| `min_expected_count` | `50` | Minimum expected volume before a bucket is eligible to be flagged. |
| `recent_signal_window_hours` | `24` | How long a flagged bucket keeps counting as an **open signal**. Must stay above the settling allowance below. |
| `anomaly_ingestion_settling_minutes` | `120` | How long a bucket is left unscored after it closes, to let late-arriving warehouse rows land. Also the detection latency it buys. Must stay below the open signal window above. |

The two dials you will actually reach for:

- **`sigma_threshold`** — **raise it** (e.g. to 5) to flag only larger, more confident deviations and cut noise; **lower it** (toward 3) to catch subtler swings at the cost of more false positives. It also moves the [release regression](#release-regression) volume-drop bar, which reuses this value.
- **`min_expected_count`** — **raise it** to ignore lower-traffic series and focus on your busiest ones; **lower it** to extend monitoring down to smaller events (expect more noise from them).

`recent_signal_window_hours` is a presentation dial rather than a detection one: it does not change what gets flagged, only how long a flagged bucket keeps counting as an open signal on the **Anomalies page** and in the sidebar badge. **Lower it** (say to 6) when a busy project's open count is dominated by burned-out spikes that have long since recovered — they age out of the count sooner; **raise it** when you want a full day or more of history to stay visible. The freshness horizon is floored at `3 × the series' own interval`, so shortening this window never closes a long-interval signal early — and neither does leaving it alone: on a daily grid the newest signal the detector may emit is already a bucket old, so a bare 24 hours would age out every signal such a series can produce. The floor binds whenever the window is shorter than 3 × the interval: at the default 24 hours it never binds on grids of 6 hours and finer, but a window of 6 on a 6-hour scan gives an 18-hour horizon, and any window under 3 hours on an hourly scan becomes 3 hours. For an event scope the interval is the scan's; a **catalog metric** is measured on the grid it collects on — its own `interval`, or, for a metric derived from already-collected events, the interval of the scan it reads. Every surface applies the same grid to the same metric, so the Anomalies page, the metrics list, the sidebar badge, the Overview stat and the metric's own detail page always agree on whether its signal is open. Two things that could still have split them do not: raising this window past the range picked on the metric's detail page extends that page's range back to the signal rather than hiding it (see [Monitoring detail](./feature-reference.md#monitoring-detail)), and switching a single metric's anomaly detection off closes its signal on all five at once instead of only on the project-wide ones. It is a window on *what is new*, and it stays one: a scope that is **still silent** is unresolved rather than new, so its single outage report stays open past this window no matter how low you set it — lowering the dial trims burned-out spikes, never a running outage. **Alert delivery is deliberately unaffected**: alert candidates and monitor status ignore this setting and use the default 24-hour window, floored at `3 × the series' own interval` exactly as above, so narrowing this dial can never close an alert state or make an already-notified rule fire again. A monitor on a daily or weekly scan or metric therefore reads **firing** while its latest anomaly bucket is inside that horizon, not just for the first 24 hours. One case still differs: an outage that never recovers stays open for alert delivery for as long as its scan keeps collecting, but the monitor judges only the age of the outage's onset bucket. Once that bucket passes the horizon, the monitor reads **warning** (an open alert state that is not firing) even though delivery still treats the incident as open.

One pairing is refused rather than allowed: this window must stay **strictly above** the [ingestion-settling allowance](#detection-latency). The allowance is how long a bucket waits before it can be scored at all; the window is how long a scored bucket counts as new. If the wait reaches the window, every signal is born already outside it — the Anomalies page, the sidebar badge and the Overview **Open signals** stat all read zero while alerting, which measures against the settled end of the series, keeps delivering. Saving either dial into that state returns an error naming both values instead of silently blanking the page, and the error arrives from both directions: raising the allowance under the stored window, or lowering the window under the stored allowance. The settings form shows the live bounds — with the defaults (24-hour window) the allowance may go up to 1439 minutes, and a 2-hour allowance needs a window of at least 3 hours.

### Detection latency

A signal for a given time bucket does **not** appear the moment that bucket closes. It appears up to one **ingestion-settling allowance** later — **two hours by default**. This is designed behaviour, not a stall.

The reason is that a warehouse keeps writing rows for a time interval well after that interval has ended on the clock. Pipelines batch, mobile clients buffer events offline and flush them hours later, and backfills land out of order. On a real hourly project the newest bucket grew by roughly 9% and the second-newest by roughly 6% between two consecutive scans — and **every** revision was upward. Scoring a bucket while it is still filling therefore manufactures **drops that evaporate on the next scan**: the detector compares a half-delivered count against a fully-delivered baseline and correctly concludes the count is low.

The allowance closes that hole without hiding data:

- **Collection is unaffected.** The newest buckets are still read, stored, and drawn on every chart, so the series you look at is always complete and current. Only anomaly *emission* waits.
- **The wait is converted into whole buckets** of each series' own grid, rounding up. With the 120-minute default, an hourly series withholds its 2 newest buckets, a 15-minute series withholds 8, and a daily series withholds 1 (a full day).
- **Nothing is skipped.** Each scan re-evaluates a trailing window of recent buckets, so a bucket that was too young to score last time is scored on the next run, once it has settled.

The withheld buckets are counted from the **end of the collected series**, which itself stops at the last *complete* clock interval. On an hourly scan with the default allowance that means the 09:00 bucket is still settling for the runs whose series end at 09:00 and at 10:00, and is first scored by a run whose series reaches 11:00. In practice, expect a signal to trail the bucket it describes by **the settling allowance plus the scan's own interval**, and never to arrive sooner than the next scan after that. Alerts inherit the same delay, because a rule can only fire on a bucket the detector has scored.

Alerting judges a signal's freshness against that **settled** end of the series rather than the raw one. The distinction matters: a scope that is still delivering events can never carry a signal newer than the withheld buckets, so measuring against the raw head would mark every live scope stale and leave only scopes that had gone dark able to alert at all.

**Tuning it.** The allowance is a per-project setting, `anomaly_ingestion_settling_minutes`, in **Project settings → Monitoring** as **"Ingestion settling (minutes)"**. It accepts 0–1440 minutes, and must additionally stay **strictly below** [`recent_signal_window_hours`](#tunables-and-defaults) — a bucket that waits longer to be scored than a signal stays open is stale the moment it is judged, so the whole page would read zero. tripl refuses that pair from either side rather than accept it.

- **Lower it** (down to `0`, which scores every bucket immediately) when your warehouse is genuinely real-time and you want the fastest possible detection. The cost is false drops on the newest bucket whenever a scan happens to run before ingestion finishes.
- **Raise it** when you see drops that disappear by the next scan, or when you know a pipeline lags by more than two hours. The cost is proportionally later detection — the setting *is* the latency.
- Set it near your ingestion pipeline's real worst-case lag. Note that it is rounded up to whole buckets, so on a daily grid anything above 0 withholds a full day.

:::tip
If a spike you can see on the chart has no marker on it yet, check the bucket's age against this setting before assuming detection is broken. A bucket younger than the allowance is deliberately unscored, and the next scan will score it.
:::

## Distribution drift

Volume detection answers "did the count spike or drop?" Distribution drift answers a different question: **"did the *mix* change even though the total stayed flat?"** — for example, 80% of an event's traffic suddenly arriving from a single platform when it used to be evenly split.

Nothing is compared until a scan names the columns to watch (**Scan settings → Distribution drift**), so an alert rule subscribed to the scope stays silent until one does. The rule editor and the monitor detail mark that scope inline when no scan in the project watches a column and no drift has been collected yet — see [When a scope is on but nothing feeds it](alerting.md#when-a-scope-is-on-but-nothing-feeds-it).

When a scan designates a **platform column**, Tripl also monitors each platform's
share of the same event total (`platform count / total count`) bucket by bucket.
These **platform parity** anomalies appear as before/after share badges on the
Breakdowns tab and stay separate from count-based volume markers. Breakdown
parity rows are not yet dispatched by alert rules; they are currently a
monitoring-detail signal.

For a categorical field (platform, country, app version, …) the detector compares the **composition** over a baseline window against the current window using the **Population Stability Index (PSI)**. PSI sums, across every category value, `(current_share − baseline_share) × ln(current_share / baseline_share)`. A larger PSI means the two distributions diverged more.

A raw PSI is partly an artefact of how much data the two windows hold: a field with many distinct values measured over a handful of events scores a sizeable PSI from sampling noise alone, with no real change in the mix. So before a band is assigned, Tripl subtracts the score a window of that size with that many distinct values produces on its own, and floors the result at zero. A category missing from one window is treated as "fewer than one event in this window" rather than as a fixed rate, so its weight scales with the window too. The PSI shown on the monitor detail and carried on the alert is that corrected score, so the number and the band always agree. A sparse scope is therefore no longer called *significant* for having drawn a small sample, while a genuine shift on a small window still is — a field that really went 50/50 to 90/10 over 50 events lands well above 0.25. At production volumes the correction is negligible (below 0.01 for a two-value field over 1000 events per window), so the published bands keep the meaning they had.

The corrected result is bucketed into interpretive bands:

| PSI | Band |
|---|---|
| below 0.10 | stable |
| 0.10 – 0.25 | minor |
| 0.25 and above | **significant** |

Only the **significant** band (PSI ≥ 0.25) is surfaced as a drift signal that alert rules can subscribe to. Alongside the score, the detector reports the handful of category values that moved the most (their before/after shares), so you can see *what* shifted, not just *that* it shifted. The sparse-window correction has no dial of its own — it is derived from the two window sizes and the number of distinct values — so the false-positive ratchet and `min_expected_count` still tune nothing on a drift scope.

## Variable value drift

Variable value drift compares observed values for one event with that variable's
effective documented contract: the event override when one exists, otherwise
the global `allowed_values` list. An empty effective list means no finite value
contract has been declared, so observations do not create drift.

One current row is kept per variable/event context. New scans refresh its novel
value evidence without changing a snoozed or false-positive review decision; the
read surface uses a 30-day evidence window. Accepting the drift updates the
documented contract either globally or for that event. Snooze, false-positive,
and reopen change only review state. Alert rules must opt in, and these
candidates behave as spike-like drift signals that bypass numeric volume
thresholds.

An **accepted** row is frozen instead of refreshed: its recorded values are the
set you accepted. A later scan reopens it as soon as it observes a value outside
that set — and only then, so a value you already accepted never nags again.
Without this the single row per variable/event would quietly absorb every future
novel value while still reading as resolved.

See [Variables & templates](./variables-and-templates.md) for the authoring and
review workflow.

## Release regression

Release regression watches for events that **break or vanish in a new app version** relative to the version before it — the classic "we shipped 2.4.0 and the checkout event stopped firing" problem. It only runs when a scan has an app-version column.

The test is deliberately careful about young releases:

1. **Maturity gate.** A release is only considered "active" once it carries the
   scan's configured minimum share of total traffic (5% by default) for a couple
   of consecutive buckets — this excludes the dev/tester trickle before a
   rollout. Releases are ordered by version number: SemVer values by SemVer
   precedence, and dotted-numeric values that are not strict SemVer (`15.8`,
   `15.10`, a four-part `1.2.3.4`) numerically, segment by segment, with a
   missing segment read as `0` — so `15.10` is newer than `15.9`, and `15.8` is
   newer than `15.7.4`. Only a label with no numeric shape at all (`beta`) sorts
   by text, below every numbered release. SemVer prereleases are excluded by default, and a scan can provide a
   custom prerelease pattern; an excluded build is ineligible both as the release
   under test and as the baseline, so a TestFlight build that happens to take
   real traffic cannot be judged or judged against. At least two eligible active
   releases must exist to compare, and the newest one must have accumulated a
   minimum total volume before it is judged at all.
2. **Fair comparison.** Counts are normalized by each release's **adoption share**, so a young release with few users isn't unfairly compared head-to-head against a mature one. For each event, the **expected** count under the new release is the previous release's share of that event applied to the new release's total volume.
3. **Evidence gate.** An event is only tested once *both* its expected count and
   the number of times it was actually seen in the previous release clear an
   absolute floor (30 events). The floor counts events rather than share of the
   baseline, because a share is a statement about how finely the catalog is
   partitioned: in a 2488-event catalog nothing reaches a percent of a release,
   and a share floor there silently drops most of the events that have plenty of
   evidence to judge. Both halves are needed because the expected count scales
   with the traffic ratio between the two releases — when a rollout carries many
   times the volume its ageing baseline still has, an event seen a handful of
   times can imply an expectation well past the floor.
4. **Comparability gate.** In the first hours of a rollout the two releases are
   not drawn from the same population — everyone on the new build is a fresh
   install working through onboarding — and normalizing by composition then makes
   every steady-state screen look halved. When too much of the new release's
   volume sits in scopes the baseline barely visited, the comparison is withheld
   and the panel says **"cannot be judged yet"** with the reason, instead of
   reporting a clean release. Events that went *completely* silent are still
   reported: a different mix of users cannot manufacture those.
5. **Verdict.** The ratio of observed to expected decides the outcome. If an event has nearly disappeared (observed far below expected — under ~5% of expected) it is classed as **missing**; if it merely dropped substantially (roughly half or less of expected) *and* the shortfall is also large in statistical terms — observed below `expected − sigma × √expected` — it is classed as a **volume drop**. The `sigma` here is the project's own `sigma_threshold` from **Detection settings** (default `4.0`), not a fixed 3: the release test deliberately shares the anomaly detector's sensitivity dial, so raising `sigma_threshold` to quiet volume anomalies also demands a larger shortfall before a release is flagged as a volume drop: at a high sigma, an event with a small expected count may no longer clear the volume-drop bar at all. A project that has never opened its detection settings uses `3`. Scope overrides from the false-positive feedback are *not* applied here. Anything in between is not flagged. Only deficits are tested; an event firing *more* in the new release is not a regression.

### Release markers on charts

The maturity gate above also draws the release onto the charts. The first time
a version becomes active, the metrics worker adds one project-level annotation,
**Release *version***, on the bucket where it crossed the gate — so a drop that
starts with a rollout sits next to the marker for it. The first version the
scan sees gets none, nor does one already live when it started looking, because
there is nothing it visibly replaced; each release gets one marker per project at
most, however many times the scan re-runs. Prereleases are never marked. The markers are
muted, carry a tag icon, and hide with **Show releases** — see
[Chart annotations](./feature-reference.md#chart-annotations).

### Where to see it

Two surfaces, and they answer different questions.

**The By version tab** on an event's or event type's monitoring page shows the
check for the **current latest release**: every regressed scope in the scan,
with the comparability verdict and the reason when a comparison is withheld.
These rows are recomputed from scratch on every scan and keep no history, so the
tab always describes the newest rollout — and it does so whatever window the run
that recomputed them covered. The check is anchored on the **newest version
bucket this scan has stored**, not on the window being collected, so **Replay a
period…** over a past period refreshes the verdict for the release that is
current *now* rather than replacing the tab with whichever release was newest
inside that period. A release-regression
incident in the Alerting Inbox links to the monitoring page of whatever it was
found on — **view event volume** for an event, **view event type volume** for an
event type — so this tab is one click from there, with the same caveat that it
describes the newest rollout only.

**An alert's own row** in Observe → Alerting → Delivery log
(`/p/<slug>/alerting`) shows a *past*
regression exactly as it was reported: scope, actual, expected and percentage,
frozen at delivery time. That is where a release-regression alert's `details:`
link goes — deliberately, because the numbers it quotes cannot be reproduced
anywhere else once the next release ships. See
[Release-regression items](alerting.md#release-regression-items).

Note that the window is not the chart's window. The comparison is measured over
the **rollout overlap** — from the point the new release became active (or 14
days back, whichever is later) up to the latest bucket — which is typically
hours or days, not the 7 days a monitoring chart shows by default. Two different
windows over two different populations is why the event chart cannot corroborate
a release-regression alert.

## Metrics

User-defined **metrics** are watched by the very same detector, at a dedicated **metric scope**. The one twist is the *shape* of the series. Each metric is classified as either **count-shaped** (a count or sum — it behaves just like an event volume) or **fractional** (a ratio, an average, or a free-form SQL value).

- **Count-shaped** metrics keep the standard treatment and the
  `min_expected_count` gate. For a metric derived from already-collected events,
  a missing bucket is zero-filled only when scan-job coverage proves the interval
  was actually collected — coverage read from the scan *that metric reads*, on
  that scan's own grid, not from whichever scan happens to be running — so
  uncovered collection gaps are omitted and an outage in collection does not
  become a fake traffic drop. A metric that collects on its own `interval` (a
  fact or SQL metric) is produced by no scan job at all, so it has no scan-job
  coverage to consult and its missing buckets are zero-filled unconditionally.
- **Fractional** metrics drop the zero-fill: a gap means "no data for this bucket" rather than zero — a ratio whose denominator was zero produces *no value at all*, and so does a free-form SQL metric that returns NaN or infinity (say, its own `0/0`) — such a bucket is skipped, never scored as an anomaly. The minimum-count gate is not so much lifted as re-read, against the **size** of the expected value rather than its sign. A ratio that naturally sits below 1, or a sparse average, is neither silenced nor constantly flagged as "too low"; and a metric that legitimately sits below zero — a signed sum, an average over a signed column, a free-form SQL level — is watched exactly like the same shape above zero. Only an expectation sitting at zero is gated out. An alert rule's own `min_expected_count` and percentage thresholds read the same magnitude, so a signed metric can reach a rule rather than being filtered out for its sign.

Per project, **`detect_metrics`** turns the metric scope on or off (the
**Metrics** box in Detection settings); per alert rule, **`include_metrics`**
decides whether metric anomalies are actually delivered — the **Metrics** box in
the rule editor, off by default (see [Alerting](./alerting.md)).

Turning the metric scope off stops those series being scored and clears only the
anomalies inside the re-evaluation window — for a catalog metric that is at
least the last 30 buckets of the metric's **own** interval, not the running
scan's. Anything older stays on the chart as history, the same promise the
per-metric anomaly toggle makes. **Reset anomalies** (**Workspace settings →
Project → General**) remains the only action that deletes recorded history,
together with switching the project's master **Anomaly detection** off. Its
confirm counts what it will delete first — "Permanently delete 12 anomalies and
3 breakdown anomalies" — from a dry run of the same window (`dry_run: true` on
`POST /projects/{slug}/danger/reset-anomalies`, and likewise `reset-drifts`).

Everything else — the seasonal baseline, the robust spread and its floor, the
z-score, and false-positive self-tuning — works exactly as it does for events.

## From a detected anomaly to a signal

A flagged bucket is written as an anomaly record carrying its scope (project
total / event type / event / metric), bucket, actual count, expected count, raw
and effective spread, detector kind, z-score, and direction. The detector
**replaces** the records for the window it just evaluated on every run, so each
scan reflects the current state of the data rather than accumulating stale
flags.

Metric series and anomaly API responses return bucket timestamps in UTC RFC
3339 form with an explicit `Z` suffix, for example `2026-09-24T08:00:00Z`.

In the app, 15-minute, hourly and 6-hour buckets are shown in your local time,
the same as signal cards, annotations and every other timestamp. Daily, weekly
and monthly buckets are calendar buckets cut in UTC, so they are labelled by
their UTC date. The hour × weekday heatmap is built from UTC buckets and is
labelled UTC.

These records become the **signals** you see on the monitoring views, and they
are the candidates the alerting layer evaluates. Schema, distribution, and
variable-value drift plus release regression feed the same machinery as
additional candidate types.

Triaging those candidates is not symmetric. Snooze, false-positive and reopen
only move review state, but **accepting** a schema drift edits the tracking plan
— on a `missing_field` drift it deletes the declared field, and tripl answers
`409 Conflict` rather than delete a field a scan builds its event names
from. See [Schema drift](./feature-reference.md#schema-drift) before you accept
one.

A latest-scan signal remains open only while it is fresh in wall-clock time:
`max(recent_signal_window_hours, 3 × the series' own interval)` — 24 hours and
the scan interval by default, or the catalog metric's own collection interval
where the series is a metric rather than an event scope. This prevents a stopped scan from pinning its
last anomaly red forever. The one exception is a scope that is **still silent
and had a non-zero expectation to lose**: because its outage is reported once and
never re-announced, ageing that single report out would erase a running incident,
so it stays open until the scope emits again or its scan stops collecting
altogether (see [An event that goes silent is reported
once](#an-event-that-goes-silent-is-reported-once)). A report whose expected
count was itself zero gets no such exemption — nothing was lost, so it ages out
on wall-clock time like every other signal. When the same scan/bucket/direction fires at project,
event-type, and event scopes, that is one **incident**. The **Anomalies page**
uses the *expanded* active-signals view: it lists every co-firing scope — project
total, each event type, and each event — and tags the child rows `within total
spike` or `within total drop`, matching the parent's direction, so you can see the
full breakdown of a spike or drop. Each row states its
size as a **% change** from the expected count (with Minor / Significant / Major
beside it); the z-score is on hover. On a phone the row drops the **Spike on** /
**Drop on** prefix (and the `within total` tag) to leave the scope name room; the
arrow beside it carries the direction. A signal an alert rule routed into an
incident shows an **Incident · *status*** link to that incident's card on the
Alerting page. Each row ends in a **⋯** menu (**Signal actions**): **Open
detail**, **View alerts** (the Alerting page, on the signal's incident when it
has one) and, for editors, **Annotate**, which opens the detail page's Volume tab
with the annotation form prefilled on the signal's bucket.

From the `sm` breakpoint up, each row also draws a **Trend** sparkline: the 24
buckets around the flagged one — the 19 before it, the bucket itself, and up to
4 after — with the flagged bucket marked, so a one-off spike reads differently
from a level that stayed put. A catalog-metric row has none, since its series
does not live on a scan. The page fetches every row's sparkline in one request,
`POST /projects/{slug}/anomalies/signals/series`, whose body lists up to 500
`{scan_config_id, scope_type, scope_ref, bucket}` scopes. The *collapsed* view behind
the sidebar and top-bar badge instead keeps the single project-total row and
counts the incident once. Either way the underlying per-scope rows stay in the
store for drilldown.

The expanded view is requested with `expanded=true` on
`GET /projects/{slug}/anomalies/signals`; each returned signal carries an
`incident_child` flag that is `true` for the child scopes folded under a
project-total incident (always `false` in the collapsed view, which omits them).
Expanded signals also carry **`incident_id`** and **`incident_status`**: the
Alerting Inbox incident an alert rule routed the signal into, and its status there
(`open`, `acknowledged`, `resolved`, `muted` or `false_positive`; a mute that has
lapsed reads `open`). Both are `null` when no rule delivered the signal, and
always `null` in the collapsed view. The Anomalies page uses them for each row's
**Incident** link.

Opening a row lands on the scope's **detail page**, where the flagged bucket is
summed up in one sentence rather than a grid of raw figures: *"Sep 25, 6:00 PM:
5,767 events, 82% above the expected 3,174 (16.0σ)."*, followed by a **Why
flagged:** line (how far from the expected value it was, against the scope's
threshold when the page knows it; a drop to zero or a bucket with no baseline
says so instead of quoting a σ). The signal banner offers **Annotate** (the
Volume tab, with the annotation form prefilled on the flagged bucket),
**Discuss** (the event's discussion thread) and **View alerts**, and the page
header carries a **View alerts** link to the Alerting page too. On a
project-total or event-type page, which has no discussion, the signal summary
carries its own **Annotate**, and so does a metric's page. The **Volume**
chart's caption says how fresh the series is — *Hourly · newest bucket 12m ago ·
collected 8m ago · next Sep 26, 3:00 PM*, or *next collection due now* — from
`last_collected_at` and `next_collection_at`, which the event, event-type and
project-total metrics responses now carry. A metric's page leaves the caption
out: its Definition already names the cadence and the next update. An annotation
placed after the newest collected bucket is drawn on that newest bucket until
the next collection catches up; the toast confirming the annotation says so.

Every signal from that endpoint — and from the `POST
/projects/{slug}/anomalies/signals/query` variant — also carries **`scope_name`**:
the display name of the thing that fired, resolved server-side to the event's
name, the event type's display name, or the catalog metric's display name. It is
`null` for `project_total`, which is named by the project rather than by a
lookup, and for an entity that was deleted out from under the anomaly row. A
client can therefore label a row from the signal alone, without downloading the
event catalog to build an id → name map. **Do not fall back to `scope_ref` when
`scope_name` is null** — `scope_ref` is a routing key, and a hex prefix printed
where a name belongs reads as a name (*"Spike on Event d4c684dd"*).

Signals from those endpoints also carry **`relative_effect`**: how big the
signal is relative to what was expected, and the value both the Anomalies page's
"Significant" magnitude filter and the sidebar badge gate on (at `0.5`). It is
computed server-side because only the server knows whether a catalog metric's
series is count-shaped, which decides whether the denominator is floored at 1:
for a count it is `|actual − expected| / max(expected, 1)`, so a scope with
essentially no traffic cannot outrank a real move on a busy one; for a
fractional metric series that floor would be a category error, so it is
`|actual − expected| / expected`. `null` means the path that produced the signal
did not compute it — fall back to your own count-shaped estimate rather than
treating the signal as having no magnitude.

### Triaging a signal that is not an incident {#triaging-a-signal}

A signal that an alert rule routed to an incident is triaged in the
[Alerting Inbox](./alerting.md), and its Anomalies row keeps an **Open incident**
action. Every other signal can be triaged from the **⋯** menu at the end of its
row on the Anomalies page:

- **Acknowledge** — you have seen it. The signal stays listed and counted, marked
  *Acknowledged*. **Undo acknowledge** removes the mark.
- **Mute this scope** — for **24 hours**, **7 days** or **until unmuted**. Every
  signal on that scope (the same event, event type, project total or catalog
  metric, on the same scan) is hidden while the mute lasts, including ones that
  open later. A lapsed mute stops hiding anything on its own. Muting again
  replaces the previous length; **Unmute scope** ends it.
- **Mark as expected…** — the move has a known cause, such as a deploy or a
  campaign. Add an optional note, and tripl writes a slate *Expected* annotation
  on the chart at that bucket with the note as its text, then hides that one
  signal. **Undo expected** removes both the mark and the annotation.

Muted and expected signals are **hidden**: they leave the open list and every
count built from it — the sidebar badge, the Overview **Open signals** headline
and the notifications bell. On the Anomalies page, **Show hidden (n)** puts them
back in the list, marked *Muted* or *Expected*, so a verdict can be reviewed or
undone. Triage changes nothing about detection itself: the thresholds stay where
they were, and an acknowledged, muted or expected signal is scored exactly as
before. A verdict given before a rule routed the signal to an incident is
ignored from then on; the inbox decides.

Triage needs the editor role. Each action is recorded in the project's audit log
(`signal.acknowledge`, `signal.mute`, `signal.mark_expected` and their undos).
Through the API, the verdicts are `POST` / `DELETE` on
`/projects/{slug}/anomalies/signals/acknowledge`, `…/mute` and `…/expected`,
keyed like the signal (`scan_config_id` — `null` for a catalog metric —
`scope_type`, `scope_ref` and `bucket`); a mute takes `duration` (`24h`, `7d` or
`until_unmuted`) and an expected mark an optional `note`. A signal routed to an
incident answers `409`. Expanded signals carry the result as
`acknowledged_at`, `muted`, `muted_until` (`null` also when muted until
unmuted), `expected`, `expected_note` and `hidden`; the collapsed list drops
hidden signals outright.

### Alert rules are an additional gate

Detection deciding a bucket is anomalous is **not** the same as you getting notified. Each alert rule applies its **own** set of gates on top of detection before anything is delivered:

- **Scan** — a rule watches every scan in the project by default, or can be bound
  to one scan (`scan_config_id`), which is the only way to keep a single noisy
  scan out of a channel. A scan-bound rule never delivers metric anomalies,
  because a catalog metric series belongs to the project rather than to a scan.
- **Scope toggles** — a rule can subscribe to project totals, event types, and/or
  individual events, and must explicitly opt in to metric anomalies
  (`include_metrics`) and to schema-drift, distribution-drift,
  variable-value-drift, and release-regression signals (all off by default).
- **Direction** — a rule can choose to notify on spikes only, drops only, or both.
- **Its own thresholds** — a minimum expected count, a minimum absolute change, and a minimum percent change, all of which the anomaly must clear *in addition to* the detector's own thresholds.
- **Cooldown** — a rule won't re-fire for the same scope until its cooldown window has passed.

So the detector's `sigma_threshold` and `min_expected_count` decide what is *flagged*; the alert rule's thresholds decide what is *delivered*. Tightening either layer reduces noise. See [Alerting](./alerting.md) for configuring rules, and the [Feature reference](./feature-reference.md) for the full field list.

### False positives self-tune the thresholds — per scope {#false-positives-self-tune-the-thresholds}

When you mark an alert in the inbox as a **false positive**, the system doesn't just dismiss it — it **automatically nudges the detector to be stricter on the scope that produced it**. Each false-positive action raises that scope's `sigma_threshold` by 0.5 (capped at 10) and its `min_expected_count` by 5 (capped at 1000). In effect, telling the system "this wasn't real" teaches it to demand a larger, higher-volume deviation *from that series* next time.

The tuning is stored as a **scope override**: an absolute pair of values that replaces the project settings for one scope only. A scope is exactly what an anomaly is keyed by — the scan plus the project total, event type, event, or catalog metric it was raised on. Marking one noisy event a false positive therefore leaves every other event, event type, project total and metric on the sensitivity you chose. (Catalog metrics are project-wide, so their overrides are not tied to a scan.)

Two details worth knowing:

- Repeat clicks on the same scope **compound**: a second false positive on the same series is a second step, not a reset.
- Schema-drift, distribution-drift, variable-value-drift and release-regression alerts also reach the inbox, but nothing scores them with these two dials, so marking one a false positive closes the incident **without** changing any detection threshold.

**Undoing it.** The ratchet is permanent — it never decays on its own. Every scope it has tightened is listed under **Settings → Monitoring → Scope overrides**, showing the scope, the scan, the values in force, and how many false positives produced them. **Remove** an override and that scope goes straight back to the project settings; nothing else moves. The project-wide `sigma_threshold` and `min_expected_count` are never changed by this feedback — they remain whatever you set. An empty list there means what it says: if the list cannot be loaded the card reports the error and offers a retry, rather than telling you nothing has been tightened.

**False positive is the "the detector is wrong here" lever, and it is the only
inbox action that changes detection at all.** Acknowledging or muting the same
incident buys silence and leaves both thresholds exactly where they were, so the
next occurrence is scored identically and alerts identically. Pick between them by
intent, not by which button is nearest —
[Silencing an incident](./alerting.md#silencing-an-incident) sets the levers side
by side.

:::tip Troubleshooting
If a series you expect to be watched is never flagged, the usual causes are: detection is disabled, the series sits below `min_expected_count`, the series is too young for a phase baseline (and too sparse for the rolling fallback), or a false-positive **scope override** has raised the thresholds for that series (check **Settings → Monitoring → Scope overrides**). If instead it is flagged but *late*, that is the [ingestion-settling allowance](#detection-latency) — the newest buckets are deliberately held unscored until they have settled. See [Troubleshooting](./troubleshooting.md).
:::
