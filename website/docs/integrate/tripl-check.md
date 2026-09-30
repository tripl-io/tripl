---
title: Check code against the plan (tripl check)
---

# Check code against the plan: `tripl check` {#tripl-check}

The plan and the code drift apart because nothing checks one against the other.
A misspelled event name, a field the plan does not know, or a value outside a
property's documented set is cheap to fix in a pull request. Found in the
warehouse a week later, it costs a backfill and a broken chart.

`tripl check` reads your tracking calls and asks the tripl instance whether each
one matches the plan. It works in two modes:

- **Static** (the default): it scans source files for tracking calls, pulls out
  the event name and the field values written as literals, and validates them.
- **Payload** (`--payloads FILE`): it validates events that were actually sent,
  captured from tests or a staging run.

Both modes send what they find to the same server-side validator,
[`POST /projects/{slug}/plan/validate`](./agent-api-guide.md#plan-validation), so
the CLI, a CI job and your own tools all get the same verdict. The command only
reads the plan. A read-only `tk_r_` key is enough.

For the flags, see [`tripl check`](../run/cli.md#tripl-check) on the CLI page.

## How it finds your tracking calls {#how-it-finds-calls}

Every app tracks events in its own way. Most teams wrap an SDK in a function of
their own, and different kinds of events often go through different wrappers.
For example, one wrapper sends structured events, another sends screen views,
and older code calls the SDK directly with a flat event name. So you configure
the check **per plan event type**. For each type, you say which functions track
it and which argument carries which plan field.

**Your own wrapper functions come first.** They are the main part of the
config. Presets for the Segment, Amplitude and Snowplow SDKs are included for
code that calls an SDK directly (see [Presets](#presets)).

The scanner does not compile your code. It reads calls as text:

- It matches a configured function name, then reads the whole argument list up
  to the matching closing parenthesis, even when the call spans several lines.
- It reads string literals, including escapes and interpolation.
- It reads named arguments (Swift argument labels, Kotlin named arguments,
  object keys in a JS object literal) and positional ones.

It supports these languages, chosen by file extension:

| Language | Extensions |
|----------|------------|
| Swift | `.swift` |
| Objective-C | `.m`, `.mm`, `.h` |
| Kotlin | `.kt`, `.kts` |
| Java | `.java` |
| TypeScript / JavaScript | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` |

A value is **literal** when the scanner can read it from the source: a string
literal, a number, or an enum case whose raw value it can find (see
[Enum sources](#enum-sources)). Anything else is **dynamic**: a property, a
function call, or a value computed at run time. A dynamic value is sent as
`null`, which means "unknown at scan time". A dynamic value is never an error.

A call where nothing at all is literal (no name, no field value) is not sent
to the validator. It is counted with status `dynamic`, and listed only with
`--strict`.

## The config file {#config-file}

The check reads the nearest `.tripl/check.yml` (or `check.yaml`, or
`check.json`) in the current directory or a parent directory. The search stops
at the repository root, the first directory that holds `.git`. Use
`--check-config PATH` to read another file. The file holds no secrets: the
instance URL and API key come from the usual
[CLI configuration](../run/cli.md#configuration) (`TRIPL_BASE_URL`,
`TRIPL_API_KEY`).

Here is a full example for an app that:

- sends structured events (`category` / `action` / `label`) through its own
  wrapper,
- sends screen views through a second wrapper,
- still has older code that calls a flat `logEvent("name", parameters: …)`,
- has a few direct Segment calls left over from a migration.

```yaml
# .tripl/check.yml
project: shop                 # project slug; --project overrides it
branch: checkout-redesign     # optional; --branch overrides it. Omit to check against main.

sources:
  - "App/**/*.swift"
  - "App/**/*.m"
  - "web/src/**/*.ts"
exclude:
  - "**/Tests/**"
  - "**/*.generated.swift"
  - "web/src/**/*.test.ts"

# Where rawValues for enum shorthand (.checkout) and qualified cases
# (AppEvents.Category.checkout.rawValue) are declared.
enums:
  - file: "App/Analytics/AppEvents.swift"
    languages: [swift]

event_types:
  # Plan event type "se": structured events. Its name rule is
  # "{category}:{action}:{label}", so the three fields together are the identity.
  se:
    calls:
      # Swift: Analytics.shared.log(category: .checkout, action: "tap", label: "pay_button",
      #                             properties: ["plan": "annual"])
      - function: "Analytics.shared.log"
        args:
          category: category
          action: action
          label: label
          properties: properties
      # Objective-C: [MyTracker trackWithCategory:@"checkout" action:@"tap" label:@"pay_button"];
      - objc_selector: "trackWithCategory:action:label:"
        positional: [category, action, label]
      # TypeScript: track("checkout", "tap", "pay_button", { plan: "annual" })
      - pattern: "^(tracker\\.)?track$"
        positional: [category, action, label, properties]

  # Plan event type "page": screen views, identified by type and id.
  page:
    calls:
      # Swift: Analytics.shared.screen(type: "paywall", id: offer.id, extra: ["source": "onboarding"])
      - function: "Analytics.shared.screen"
        args:
          type: type
          id: id
          extra: properties

  # Plan event type "legacy": flat event names, no name rule.
  legacy:
    calls:
      # Swift: Analytics.shared.logEvent("promo_banner_shown", parameters: ["slot": "home"])
      #        Analytics.shared.logEvent("promo_sheet_\(sheetId)_shown")
      - function: "Analytics.shared.logEvent"
        name_arg: 0
        args:
          parameters: properties
      # Objective-C: [MyTracker logEvent:@"promo_banner_shown"];
      - objc_selector: "logEvent:"
        name_arg: 0

  # Plan event type "track": events sent straight to Segment.
  track:
    preset: segment_track
```

### Keys {#config-keys}

| Key | Meaning |
|-----|---------|
| `project` | The project slug. **Required** unless you pass `--project`. |
| `branch` | Optional. A plan branch, by name or id, matched exactly the way [`tripl plan --branch`](../run/cli.md#the---branch-flag) matches it. Omit it to check against the live main plan. `--branch` on the command line overrides it. |
| `root` | Optional. The directory that `sources`, `exclude` and `enums` globs start from, relative to the base directory. The base directory is the one that holds `.tripl/` (for a config file outside a `.tripl/` directory, the file's own directory). Default: the base directory. |
| `sources` | Glob patterns for the files to scan, relative to `root`. `**` matches any number of directories, and `{a,b}` matches either alternative. Default: `**/*`, every file in a supported language. |
| `exclude` | Glob patterns for files to skip, even when a `sources` pattern matches them. They are added to the built-in excludes: `.git`, `node_modules`, `Pods`, `Carthage`, `DerivedData`, `.build`, `build` and `dist` directories, and `*.min.js` files. |
| `enums` | Files that declare the enums your calls use: a glob string, or `{file: GLOB, languages: [...]}`. See [Enum sources](#enum-sources). |
| `event_types` | One entry per **plan event type name** (the name `tripl plan types` prints). Each entry has `calls`, a `preset` (or a list under `presets`), or both. With both, your own `calls` are tried first. An entry with neither is a config error. |

An `event_types` entry can also have `field_map`, which renames targets to
plan field names for every call of the type, presets included. Use it when a
preset's field names differ from your plan's (see [Presets](#presets)):

```yaml
event_types:
  se:
    preset: snowplow_structured
    field_map:
      category: event_category   # the preset's `category` is the plan field `event_category`
      action: event_action
```

Each item under `calls` describes one way this event type is tracked:

| Key | Meaning |
|-----|---------|
| `function` | A dotted callee, for example `Analytics.shared.log`. It matches whole segments from the right, so `Analytics.shared.log` also matches `app.Analytics.shared.log`, but not `MyAnalytics.shared.log`. The match is case-sensitive. Whitespace, `()` and `[]` in the callee are ignored. |
| `pattern` | A regular expression (Python syntax) that must match the **whole** normalised callee. The callee is normalised by dropping whitespace, `?` and `!`, and the arguments of intermediate calls, so `Amplitude.instance().logEvent` reads `Amplitude.instance.logEvent`. Use it when the receiver varies (`tracker.track`, `this.tracker.track`). |
| `objc_selector` | An Objective-C selector, for example `trackWithCategory:action:label:`. It matches a message send with exactly these selector parts. |
| `receiver` | Only with `objc_selector`: a regular expression the receiver must contain (`re.search`), for example `(?i)tracker`. |
| `args` | Maps an **argument label** (Swift label, Kotlin named argument, a key of the `object_arg` object, or the label of an Objective-C selector part) to a **target**. |
| `positional` | Targets for unlabelled arguments, in order. For `objc_selector`, targets for the selector's arguments, in order. |
| `chain` | Builder method to target, for calls followed by a chain such as `.label("x")`. |
| `object_arg` | The position of an argument that is an object literal of labelled values, as in `trackStructEvent({category, action})`. Nested keys read as `a.b`. |
| `name_arg` | Shorthand for the target `name`: an argument position (`0` is the first argument) or a label. |
| `properties_arg` | Shorthand for the target `properties`, the same way. |
| `languages` | Optional. Limit the call to these languages, for example `[swift, objc]`. |

A **target** is one of:

- a plan field name;
- `name`: the event's name or identity, for event types identified by a flat name;
- `name:iglu`: the event name inside an Iglu schema URI (`iglu:com.acme/button_click/jsonschema/1-0-0` gives `button_click`);
- `properties`: a free-form dictionary. When it is a literal, its keys are
  checked as field names and its literal values like any other field value;
- `field:<x>`: the plan field literally called `name` or `properties`;
- `null`: ignore the argument.

Each call needs exactly one of `function`, `pattern` or `objc_selector`.
Everything else is optional. Without `args`, a label maps to the field of the
same name, except the usual labels for the name (`name`, `event`, `eventName`,
`event_name`, `eventType`, `event_type`) and for the property dictionary
(`properties`, `props`, `parameters`, `params`, `eventProperties`,
`event_properties`, `attributes`, `extras`). An Objective-C selector part maps
to the word after `With`, so `trackWithCategory:` gives `category`. With
`args`, only the listed labels are read.

### Structured events and flat names {#identity}

Each event in the plan has an **identity**. For an event type with a **name
rule** (the scan's event name format, for example `{category}:{action}:{label}`),
the identity is built from the field values. `category: "checkout"`,
`action: "tap"` and `label: "pay_button"` give `checkout:tap:pay_button`. That is
the event the check looks for. For an event type with no name rule, the identity
is the event's name, and you point `name_arg` at it.

If a value that the name rule needs is dynamic, it becomes a `${…}` token (a
**hole**) in the identity: `category: "checkout"`, `action: .tap` from an
unknown enum, `label: "pay_button"` give `checkout:${action}:pay_button`. A
hole matches whatever the plan has in that place, and the fields the call can
read are still validated. When the identity matches no planned event:

- **Fully literal** (no holes): `unknown_event` is an **error**.
- **Some holes, some literal text**: `unknown_event` is a **warning**. The
  scanner cannot prove the call sends an unplanned event, only that nothing it
  could read matches one.
- **Only holes** (and separators), such as `${category}:${action}:${label}`:
  the call identifies nothing, so it is not matched and gets no finding.

A payload in [payload mode](#modes) is what the app actually sent, so its
values are never holes, and an identity that matches nothing is always an
error.

## Presets {#presets}

Presets cover the common SDK calls. A preset is written in the same vocabulary
as your own `calls` and goes through the same validation. Its targets are the
canonical field names `category`, `action`, `label`, `property`, `value`, `id`
and `type`. If your plan names a field differently, rename it with
[`field_map`](#config-keys) on the event type.

A preset matches its SDK's receiver by name: `analytics.track` and
`Analytics.shared.track` match `segment_track`, while `tracker.track` does not.
Singleton accessors such as `.shared`, `.instance()` and `.getInstance()`
between the receiver and the method are allowed.

| Preset | Matches | Maps |
|--------|---------|------|
| `segment_track` | `.track(…)` on a receiver whose name contains `analytics` or `segment`: `analytics.track("Name", {…})` (JS/TS, Kotlin, Java), `analytics.track(name: "Name", properties: […])` (Swift); Objective-C `track:properties:` and `track:` sent to such a receiver | the first argument, or the `name` / `event` label, to the event name; the second, or `properties`, to `properties` |
| `amplitude_log_event` | `.logEvent(…)` or `.track(…)` on a receiver whose name contains `amplitude`: `amplitude.track("Name", {…})`, `Amplitude.instance().logEvent("Name", withEventProperties: […])`, `amplitude.track(eventType: "Name", eventProperties: […])`; Objective-C `logEvent:withEventProperties:` and `logEvent:` | the first argument, or `eventType` / `event_type`, to the event name; the second, or `eventProperties` / `event_properties` / `withEventProperties`, to `properties` |
| `snowplow_structured` | `Structured(category: "c", action: "a")` (also `SPStructured`, `StructuredEvent`, `Structured.builder()`) with chained `.label(…)` / `.property(…)` / `.value(…)`; JS `trackStructEvent({category, action, label, property, value})`; Objective-C `[[SPStructured alloc] initWithCategory:action:]` | `category`, `action`, `label`, `property` and `value` to the fields of the same names |
| `snowplow_screen_view` | `ScreenView(name: "Home", screenId: …)` (also `SPScreenView`, `ScreenView.builder()`); JS `trackScreenView({name, id})`; Objective-C `[[SPScreenView alloc] initWithName:screenId:]` | the screen name to the **event name**; the screen id (`screenId` or `id`) to the field `id`; `type` to the field `type` |
| `snowplow_self_describing` | `SelfDescribing(schema: "iglu:…", payload: […])` (also `SPSelfDescribing`, `SelfDescribingJson`); JS `trackSelfDescribingEvent({event: {schema, data}})`; Objective-C `initWithSchema:payload:` | the Iglu schema's name segment to the event name; `payload` / `data` / `eventData` to `properties` |

## Enum sources {#enum-sources}

Wrappers often take enum cases instead of strings:

```swift
Analytics.shared.log(category: .checkout, action: .tap, label: "pay_button")
Analytics.shared.log(category: AppEvents.Category.checkout.rawValue, action: "tap", label: "pay_button")
```

List the files that declare those enums under `enums`, and the scanner reads
their raw values:

```swift
// App/Analytics/AppEvents.swift
enum AppEvents {
    enum Category: String {
        case checkout                    // raw value "checkout" (the case name)
        case onboarding = "onboarding_v2"
    }
}
```

- **Shorthand** (`.onboarding`) resolves by case name across every enum the
  sources declare.
- **Qualified cases** (`AppEvents.Category.onboarding`, with or without
  `.rawValue`) resolve through the enum's name.
- A shorthand case declared in two enums with **different** raw values is
  ambiguous. The scanner treats it as dynamic rather than guess. Use the
  qualified form in that call, or rename one of the cases.
- A case the sources do not declare is also dynamic.

## Interpolated names become properties {#interpolation}

A name built with string interpolation is not a typo. The literal part is still
checked:

| Language | In code | Sent for validation as |
|----------|---------|------------------------|
| Swift | `"promo_sheet_\(sheetId)_shown"` | `promo_sheet_${sheetId}_shown` |
| Kotlin | `"promo_sheet_${sheetId}_shown"`, `"promo_sheet_$sheetId" + "_shown"` | `promo_sheet_${sheetId}_shown` |
| TypeScript / JavaScript | `` `promo_sheet_${sheetId}_shown` `` | `promo_sheet_${sheetId}_shown` |

Each interpolation becomes a `${…}` token, the same placeholder syntax the plan
uses for [properties](../use/variables-and-templates.md#use-placeholders-in-event-values).
The name is then matched against plan names that have tokens in the same
places. A token matches a token whatever its property is called, so
`promo_sheet_${sheetId}_shown` in code matches `promo_sheet_${sheet_id}_shown` in
the plan. The value behind the token is unknown at scan time, so it is treated
as dynamic.

A name that is not a string literal at all (a property, or a function that
returns the name) is dynamic. Such a call is counted but cannot be matched to an
event.

## What it reports {#findings}

Each finding has a **code**. Select on the code in scripts, never on the
message text.

| Code | Severity | Meaning |
|------|----------|---------|
| `unknown_event_type` | error | The config names an event type the plan does not have. |
| `unknown_event` | error, or warning with holes | No event in the plan has this identity or name. An error when the identity is fully literal, a warning when part of it is only known at runtime. An identity of holes alone gets no finding (see [Structured events and flat names](#identity)). |
| `deprecated_event` | warning, or error when archived | The event is `deprecated`: new code should send its successor. An `archived` event is an error. |
| `unknown_field` | warning | A field, or a key of a literal `properties` dictionary, that the event type does not define and that is not one of the event's typed properties. |
| `missing_required_field` | error | A required field, or a property the event's list marks required, is missing. **Payload mode only**: a static call may set the field somewhere the scanner cannot see. |
| `wrong_type` | error | A property's value has a different JSON type than the plan gives it, for example the string `"3"` for a `number`. |
| `value_not_allowed` | error | A literal value is outside what the plan allows: the field's enum options, the documented values of the property the field refers to, or the field's contract regex or min/max. |
| `dynamic_value` | info or warning, and only with `--strict` | A value the scanner could not read, or an event name that is only partly known. It is listed so you can see what was not checked. The validator reports it as `info`; the CLI adds a `warning` for a value the validator did not already note. |
| `too_dynamic` | info | The identity has more than 10 holes, too many to match against the plan. The call is not matched to an event. |
| `oversize_value` | warning | A name, event type, field or property was over the validator's size limits. The CLI sent the value as `null` (or dropped the key) instead of failing the batch. Raised by the CLI. |
| `no_verdict` | error | The CLI sent the item but the validator returned no verdict for it. Raised by the CLI, never by the server. |

**Typed properties.** A key of `properties` is also checked against the
event's typed properties. These are the keys of the event's JSON field
templates.

- **Nested objects.** A nested object is matched by dotted path, so
  `{"cart": {"total": 1}}` checks `cart.total`.
- **The JSON field's name.** The properties may be sent under the JSON field's
  own name, as `{"properties": {...}}`, or at the top level.
- **Allowed values.** A property's allowed values are checked the same way a
  field's are.

A `null` (dynamic) value never produces `value_not_allowed` or
`missing_required_field`. An `info` finding does not change an item's status,
except with `--strict`, where it makes an otherwise clean item a warning.

## Static and payload modes {#modes}

**Static** (`tripl check`) scans `sources` and validates each call it finds.
Findings point at the file and line of the call.

**Payload** (`tripl check --payloads FILE`) validates events that were actually
sent. `FILE` is NDJSON (one JSON object per line, blank lines ignored), a JSON
array of the same objects, or a single object. `--payloads -` reads standard
input:

```json
{"event_type": "se", "fields": {"category": "checkout", "action": "tap", "label": "pay_button"}, "properties": {"plan": "annual"}}
{"name": "promo_banner_shown", "properties": {"slot": "home"}}
{"event_type": "page", "fields": {"type": "paywall"}}
```

| Key | Meaning |
|-----|---------|
| `event_type` | Optional. The plan event type name. With it, `fields` are used to build the identity with the type's name rule. |
| `name` | Optional. The event name or identity, looked up across all types when `event_type` is absent. |
| `fields` | Plan field name to value: a string, number, boolean or `null`. Numbers and booleans are checked as their JSON text (`3`, `true`). |
| `properties` | Any other keys the event carried. They are checked as field names. |

An event needs `event_type` or `name`. Any other key is a usage error (exit 2).

A payload is a complete event, so a required field that is missing **is** an
error here (`missing_required_field`), and its values are never treated as
holes. In the example above, the third line
fails if `page` requires `id`. Payload mode needs no `sources`, `event_types` or
`enums`. It still reads `project` and `branch` from the config file if one is
present.

## Output formats {#output}

| Flag | Output on stdout |
|------|------------------|
| *(none)* | Human-readable text: every item that is not a clean pass, with its location and findings, then a summary. |
| `--json` | One JSON document with a row for **every** item, passing ones included. See the [`check` document](../run/cli.md#check-document). |
| `--format sarif` | A [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) log, for GitHub code scanning and other SARIF viewers. |

In SARIF, each finding is one result and its code is the rule (`ruleId`).
Errors are `level: "error"`, warnings `level: "warning"` and info findings
`level: "note"`. A static finding carries a `physicalLocation` with a `region`
for its line and column. Its path is relative to the scan root, the `root` of
the check configuration (by default the directory that holds `.tripl/`), which
the log declares as `%SRCROOT%`. Keep `.tripl/` at the repository root so the
paths line up with the files code scanning shows. A payload finding from a file
points at the `--payloads` path as you typed it, so a relative path is relative
to the working directory the command ran in, and at the event's line. A payload
read from standard input has no file location: its finding carries a
`logicalLocation` naming the line or array position instead. SARIF is mainly
useful for the static mode.

Human text and progress messages go to stderr when `--json` or `--format sarif`
is given, so you can redirect stdout to a file.

## Exit codes {#exit-codes}

| Code | Meaning |
|------|---------|
| **0** | No errors. Warnings are allowed unless you pass `--strict`. |
| **1** | At least one error, or at least one warning with `--strict`. Also: a request to the instance failed. In that case no document is written to stdout. |
| **2** | A config or usage error: no config file in static mode, invalid YAML, an unknown key or preset, a call without exactly one of `function` / `pattern` / `objc_selector`, a `--payloads` file that is not valid JSON or holds an event with neither `event_type` nor `name`, no project, `--json` together with another `--format`, or a `--branch` that matches nothing. |

`--strict` makes warnings fail the run, reports a `dynamic_value` finding for
every value the scanner could not read, and lists the calls where nothing was
readable (status `dynamic`), counting them as warnings. Use it in a gate you
watch, not in an unattended job, where a new dynamic value would turn the job
red.

`--branch` checks against a plan branch instead of main. Use it for a pull
request that ships code for events that are still on a plan branch under
review.

## In GitHub Actions {#github-actions}

This workflow runs the check on every pull request, fails the job on errors,
and uploads SARIF so findings appear on the changed lines. It installs the CLI
with `uv` the same way the [CLI page](../run/cli.md#install) does.

```yaml
# .github/workflows/tracking-plan.yml
name: Tracking plan

on:
  pull_request:

permissions:
  contents: read
  security-events: write   # needed to upload SARIF

jobs:
  tripl-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6

      - name: Check tracking calls against the plan
        env:
          TRIPL_BASE_URL: https://tripl.example.com
          TRIPL_API_KEY: ${{ secrets.TRIPL_READ_ONLY_KEY }}
        run: uvx tripl check --format sarif > tripl-check.sarif

      - name: Upload findings to code scanning
        if: always()   # upload even when the check failed
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: tripl-check.sarif
          category: tripl-check
```

Notes:

- `if: always()` on the upload matters. The check exits 1 when it finds an
  error, and without it the findings would never reach the pull request.
- A read-only key (`tk_r_`) is enough, because the check only reads the plan.
  If you already keep a write key in secrets for [`tripl annotate`](../run/cli.md#in-a-github-actions-workflow),
  do not reuse it here. Mint a read key and scope it to the project.
- To check against a plan branch named after the pull request's branch, add
  `--branch "${{ github.head_ref }}"`. The check exits 2 when no such plan
  branch exists, so use this only if your team follows that naming convention.
- Without `uv`, `pip install tripl` followed by `tripl check …` works the same.

To check captured events instead, for example from a UI test run that writes
what the app sent to `events.ndjson`:

```yaml
      - name: Check events captured by the UI tests
        env:
          TRIPL_BASE_URL: https://tripl.example.com
          TRIPL_API_KEY: ${{ secrets.TRIPL_READ_ONLY_KEY }}
        run: uvx tripl check --payloads build/events.ndjson
```

## Related pages {#related}

- [`tripl check`](../run/cli.md#tripl-check) on the CLI page: every flag.
- [Agent API guide: plan validation](./agent-api-guide.md#plan-validation): the
  endpoint behind the check, for your own tools.
- [Properties & templates](../use/variables-and-templates.md): documented values
  and `${variable}` placeholders.
