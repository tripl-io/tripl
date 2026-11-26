---
title: Check the events your UI tests send
sidebar_label: Check events from UI tests
sidebar_position: 13
description: Record what the app sends while its UI tests run, one JSON line per event, and check the file against the tracking plan in CI with tripl check --payloads.
---

# Check the events your UI tests send

**You will:** record every event the app sends while its UI tests run, one JSON
line per event, and check that file against the tracking plan in CI. This
finds what a scan of the code cannot: a required field that was never set, or a
name or value built at runtime.

**You need:** UI tests that drive the app (Espresso or UI Automator, XCUITest,
Playwright or Cypress), a tracking facade of your own or a Snowplow tracker,
and the read-only key from
[Check tracking code in CI](./check-tracking-in-ci.md#1-create-a-read-only-key-for-ci).

`tripl check` reads source code by default. With `--payloads FILE` it checks
captured events instead: what the app actually sent. A captured event is
complete, so a required field that is missing **is** an error
(`missing_required_field`), which a scan of the code never reports. See
[Static and payload modes](../integrate/tripl-check.md#modes) for the rest.
tripl ships no recorder: the app writes the file itself, in its debug or test
build, with a few lines around the code that already sends events.

## 1. The file to write {#format}

One JSON object per line (NDJSON), one line per event the app sent. Blank lines
are ignored:

```json
{"event_type": "se", "fields": {"category": "checkout", "action": "tap", "label": "pay_button"}, "properties": {"plan": "annual"}, "ref": "CheckoutTest.payWithCard"}
{"name": "promo_banner_shown", "properties": {"slot": "home", "position": 2}, "ref": "HomeTest.banner"}
{"event_type": "page", "fields": {"type": "paywall"}, "ref": "PaywallTest.open"}
```

| Key | Meaning |
|-----|---------|
| `event_type` | Optional. The plan event type, by the name `.tripl/check.yml` gives it. With it, `fields` build the event's identity with the type's name rule. |
| `name` | Optional. The event name or identity, looked up across all types when `event_type` is absent. |
| `fields` | Optional. Plan field name to value: a string, number, boolean or `null`. Numbers and booleans are checked as their JSON text (`3`, `true`). |
| `properties` | Optional. Every other key the event carried. Each is checked as a field name of the type. |
| `ref` | Optional. Where the event came from, for whoever reads the file: the test, the screen or the step. It is never checked or sent, and two events that differ only in it are one. |

A line needs `event_type` or `name`. Any other key, or a line that is not a JSON
object, stops the run with exit 2 before anything is sent.

What goes in `fields` and what in `properties`:

- **`fields`** are the values the plan's event type defines as fields, and
  above all the ones its name rule reads (`category`, `action` and `label` in
  `{category}:{action}:{label}`). Write them with the plan's field names.
- **`properties`** are everything else the event carried.
- A value that changes on every run (a screen ID, a timestamp, a session or
  order ID) belongs in `properties`. A
  [payload baseline](../integrate/tripl-check.md#baseline) keys an event on its
  field values but only on the names of its properties, so a field that changes
  every run makes the event new every run.

## 2. Record from your own tracking facade {#facade}

Most apps send events through one facade of their own (`Analytics.track(name,
params)`), which then calls one or more SDKs. Wrap it, in debug and test builds
only, with a decorator that writes one line per event and passes the event on.
The snippets below are examples to adapt to your facade, not a library: tripl
does not build or test them.

**Android (Kotlin).** Install the decorator in the debug build, write the file
under the app's external files directory, and pull it after the tests:
`adb pull /sdcard/Android/data/<package>/files/events.ndjson build/events.ndjson`.

```kotlin
// Debug builds only: every event goes to the file, then to the real tracker.
class RecordingTracker(
    private val inner: Tracker,
    private val file: File,
    var ref: String? = null, // set by the test, e.g. in a TestWatcher rule
) : Tracker {
    override fun track(name: String, params: Map<String, Any?>) {
        val line = JSONObject().put("name", name).put("properties", JSONObject(params))
        ref?.let { line.put("ref", it) }
        synchronized(this) { file.appendText("$line\n") }
        inner.track(name, params)
    }
}
```

**iOS (Swift).** A UI test runs the app in another process, so pass the file's
path, and the test's name as the `ref`, in the app's `launchEnvironment`. On the
simulator the app can write to a path on the Mac that runs the tests.

```swift
// Debug builds only: every event goes to the file, then to the real tracker.
final class RecordingTracker: Tracking {
    private let inner: Tracking
    private let url: URL
    private let ref = ProcessInfo.processInfo.environment["TRIPL_EVENTS_REF"]

    init(inner: Tracking, path: String) {
        self.inner = inner
        url = URL(fileURLWithPath: path)
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
    }

    func track(_ name: String, properties: [String: Any]) {
        var line: [String: Any] = ["name": name, "properties": properties]
        if let ref { line["ref"] = ref }
        if let data = try? JSONSerialization.data(withJSONObject: line),
           let file = try? FileHandle(forWritingTo: url) {
            file.seekToEndOfFile()
            file.write(data + Data("\n".utf8))
            try? file.close()
        }
        inner.track(name, properties: properties)
    }
}
```

**Web (TypeScript, Playwright).** The page hands each event to a function the
test exposes, and the test writes the line. An exposed function survives
navigations, so no event is lost when the page reloads.

```typescript
// In the app, test builds only: hand every event to the test, then send it.
export const recording = (inner: Tracker): Tracker => ({
  track(name, properties) {
    (window as any).__triplRecord?.({ name, properties });
    inner.track(name, properties);
  },
});

// In the Playwright tests: one line per event, with the test's title as its ref.
test.beforeEach(async ({ page }, testInfo) => {
  const ref = testInfo.titlePath.slice(1).join(" > ");
  await page.exposeFunction("__triplRecord", (event: object) =>
    fs.appendFileSync("build/events.ndjson", JSON.stringify({ ...event, ref }) + "\n"),
  );
});
```

When the plan's event type has fields, split them out as you write the line:
the facade usually knows which parameters are which (`category`, `action` and
`label` of a structured event, say). Start each test run with an empty file, so
that a line from an earlier run is never checked again.

## 3. Record from a Snowplow tracker {#snowplow}

The Snowplow trackers can call your code after each event they track, so the
recorder needs no facade: a plugin with an `afterTrack` callback
(`PluginConfiguration` in the Android and iOS trackers 5 and newer, `addPlugin`
in the JavaScript tracker 3 and newer). Add it to the tracker in test builds
only. The callback gets the event's schema and its payload; map them as the
[Snowplow presets](../integrate/tripl-check.md#presets) of a static check do:

| Snowplow event | What the callback sees | The line to write |
|----------------|------------------------|-------------------|
| Structured | `se_ca`, `se_ac`, `se_la`, `se_pr`, `se_va` in the payload (the JavaScript tracker: event `e` = `se`) | `event_type`: the type `.tripl/check.yml` maps the structured preset to; `fields`: `category`, `action`, `label`, `property`, `value` |
| Self-describing | the schema `iglu:com.acme/promo_banner_shown/jsonschema/1-0-0` and its data (the JavaScript tracker: event `ue`, the data in `ue_pr`) | `name`: the schema's name segment, `promo_banner_shown`; `properties`: the data |
| Screen view | the schema `iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0` and its data | `event_type`: the type of the screen view preset; `name`: the data's `name`; `fields`: `type`, if the plan defines it; `properties`: the rest, the screen `id` among them |

The three, written out:

```json
{"event_type": "se", "fields": {"category": "checkout", "action": "tap", "label": "pay_button", "value": 1}, "ref": "CheckoutTest.payWithCard"}
{"name": "promo_banner_shown", "properties": {"slot": "home", "position": 2}, "ref": "HomeTest.banner"}
{"event_type": "screen_view", "name": "Paywall", "fields": {"type": "modal"}, "properties": {"id": "1f0e8c1a-77a4-4c43-9d1e-5c7a2b6f0b55"}, "ref": "PaywallTest.open"}
```

Leave out what the plan does not describe: the entities (contexts) attached to
an event, and the tracker's own parameters (`eid`, `dtm`, `tv`, the platform).
A field the payload does not carry (no `se_la`, say) is left out of `fields`,
never written as an empty string: the check then reports a required one as
missing.

The JavaScript tracker, in TypeScript. In the test build, set
`encodeBase64: false` in the tracker's configuration so that `ue_pr` holds
plain JSON:

```typescript
import { addPlugin } from "@snowplow/browser-tracker";

// Test builds only: one line per tracked event, through the function the test exposes.
addPlugin({
  plugin: {
    afterTrack(payload: Record<string, unknown>) {
      (window as any).__triplRecord?.(toLine(payload));
    },
  },
});

function toLine(p: Record<string, unknown>): object | undefined {
  if (p.e === "se") {
    const fields = { category: p.se_ca, action: p.se_ac, label: p.se_la, property: p.se_pr, value: p.se_va };
    return { event_type: "se", fields: Object.fromEntries(Object.entries(fields).filter(([, v]) => v != null)) };
  }
  if (p.e === "ue") {
    const { schema, data } = JSON.parse(String(p.ue_pr)).data; // the event inside the envelope
    return { name: schema.split("/")[1], properties: data };
  }
  return undefined; // page views and the rest: not in the plan here
}
```

On Android and iOS the callback receives the tracked event: its `schema`, its
name (`name` on Android, `eventName` on iOS; `se` for a structured event) and
its `payload`, a map that holds `se_ca` and the rest for a structured event and
the data of a self-describing one. Write it with the same mapping, as in the
facade examples above.

## 4. Check the file on your machine {#locally}

Run the UI tests, then the check, with the instance's address and the
read-only key:

```bash
export TRIPL_BASE_URL=https://tripl.example.com
export TRIPL_API_KEY=tk_r_...
uvx tripl@X.Y.Z check --payloads build/events.ndjson
```

Each finding names the file and the line of the event; the line's `ref` tells
you which test sent it. `--payloads` reads the path relative to the directory
the command runs in. It needs no `sources` or `event_types` in the check
config, and still reads `project` and `branch` from it.

Fix what is wrong, in the app or in the plan. What is left, record in a
baseline of its own and commit it: a baseline is written for one mode, and the
check refuses one written for the static check (exit 2).

```bash
uvx tripl@X.Y.Z check --payloads build/events.ndjson --write-baseline .tripl/check-payloads-baseline.json
git add .tripl/check-payloads-baseline.json
```

A payload baseline matches an event by presence, not by count: how many times
the tests fired it is not compared. See
[Baseline](../integrate/tripl-check.md#baseline).

## 5. Check it in CI {#ci}

The UI-test job keeps the file as an artifact, and a check job after it reads
the file. A payload run has no gate: `--new-from-rev` and `--new-from-patch`
are refused with `--payloads` (a captured event has no source to diff), so the
payload baseline decides what fails. The templates below leave out the gate,
and read `.tripl/check-payloads-baseline.json` in place of the static baseline,
whenever `payloads` is set.

### GitLab CI/CD {#gitlab}

Keep the file as an artifact of the UI-test job, then include the template a
second time, under another job name, with `payloads` and `needs`:

```yaml
ui-tests:
  stage: test
  script:
    - ./run-ui-tests.sh            # writes build/events.ndjson
  artifacts:
    when: always                   # a failed test run still hands over what it sent
    paths: [build/events.ndjson]
    expire_in: 7 days

include:
  # The check of the code, as in "Check tracking code in CI".
  - remote: https://raw.githubusercontent.com/tripl-io/tripl/cli-vX.Y.Z/ci/gitlab/tripl-check.yml
    inputs:
      base_url: https://tripl.example.com
  # The check of the events the UI tests sent.
  - remote: https://raw.githubusercontent.com/tripl-io/tripl/cli-vX.Y.Z/ci/gitlab/tripl-check.yml
    inputs:
      job: tripl-check-events
      base_url: https://tripl.example.com
      payloads: build/events.ndjson
      needs: [ui-tests]
```

- **`needs`** names the jobs the check waits for and takes artifacts from. The
  template's default, none, starts the job at once and downloads no artifact,
  so without it the file never reaches the check. The job named there must run
  in every pipeline the check runs in (by default, branch pipelines), or GitLab
  refuses to create the pipeline.
- **`payloads`** is relative to the repository root, like the other paths of
  the template, whatever `directory` says.
- **`payloads_baseline`** (default `.tripl/check-payloads-baseline.json`) is
  used when the file exists; `baseline` is not used in a payload run.
- **`job`** keeps the two includes apart: two jobs of one name would be merged
  into one.
- A UI-test run that wrote no file fails the check with exit 2. When a run may
  rightly send nothing, add `args: --allow-empty`.

### GitHub Actions {#github-actions}

Upload the file from the UI-test job, download it in a check job, and run the
action with `payloads`:

```yaml
jobs:
  ui-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: ./run-ui-tests.sh      # writes build/events.ndjson
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: tracked-events
          path: build/events.ndjson

  tripl-check-events:
    needs: ui-tests
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7   # the baseline and the check config
      - uses: actions/download-artifact@v4
        with:
          name: tracked-events
          path: build
      - uses: tripl-io/tripl-check-action@vX.Y.Z
        with:
          base-url: https://tripl.example.com
          api-key: ${{ secrets.TRIPL_READ_ONLY_KEY }}
          payloads: build/events.ndjson
```

With `payloads`, the action reads `payloads-baseline` (default
`.tripl/check-payloads-baseline.json`) in place of `baseline`, passes no gate,
and uploads its findings to code scanning under the category
`tripl-check-payloads`, so they never replace the alerts of the code's check.
They point at lines of the events file, which is not in the repository; set
`sarif: false` and read the job log instead, if you would rather keep them out
of code scanning. When the UI tests run in the same job, skip the artifact and
point `payloads` at the file they wrote.

### Any other CI {#other-ci}

Run the check after the UI tests, with the file the tests wrote:

```bash
tripl check --payloads build/events.ndjson --baseline .tripl/check-payloads-baseline.json --report junit=tripl-events.xml
```

Leave out `--new-from-rev` and `--new-from-patch`: with `--payloads` they stop
the run with exit 2. The exit codes are those of
[the static check](./check-tracking-in-ci.md#5-make-it-block-merges).

**More detail:** [Static and payload modes](../integrate/tripl-check.md#modes)
in the `tripl check` guide, and the [`--payloads`](../run/cli.md#tripl-check)
flag on the CLI page.
