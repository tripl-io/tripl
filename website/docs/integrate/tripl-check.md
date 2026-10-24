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
To run it on every merge request, in GitLab, GitHub or any other CI, see
[Check tracking code in CI](../how-to/check-tracking-in-ci.md).

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
literal, a number, an enum case or a string constant whose value it can find,
or a call to a function declared to return a string whose every `return` is a
string literal (see [Enum sources](#enum-sources)). Anything else is
**dynamic**: a local variable, a parameter, any other function call, or a value
computed at run time. A dynamic
value is sent as `null`, which means "unknown at scan time". A dynamic value is
never an error. `tripl check --list` shows what the scan reads from each call,
and why it does not read a value (see [List what the scan reads](#list)).

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
| `event_types` | One entry per **plan event type name** (the name `tripl plan types` prints). Each entry has `calls`, a `preset` (or a list under `presets`), or both. With both, your own `calls` are tried first. A `preset` and a `presets` list can be combined: the `preset` comes first. An entry with neither is a config error. |

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

To start a call spec of your own from a preset, print it.
`tripl check --print-preset segment_track` prints the preset's call specs as
YAML, after two comment lines:

```text
# preset segment_track: paste these under an event type's calls: in place of preset: segment_track, then edit them.
# If this is the event type's first preset and its codegen: block has no style:, set style: named in it; codegen took that style from this preset.
```

Paste the specs under an event type's `calls:` in place of
`preset: segment_track`, then edit them. If that was the event type's first
preset and its `codegen:` block has no `style:`, set the `style:` the second
line names in it: [`tripl codegen`](./codegen.md#styles) took the style from
the first preset's name, which the pasted specs do not carry. Otherwise leave
the block as it is: a `style:` it sets is the one codegen uses, and a preset
after the first gave no style. `--print-preset` without a name prints every
preset. It needs no config and no key.

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

**String constants** resolve the same way, by their qualified name:
`const val X = "y"` (Kotlin, also inside an `object` or an unnamed
`companion object`; a named one's constants resolve as `Outer.Name.X` or
`Name.X`, never as `Outer.X`),
`static final String X = "y"` (Java), `static let x = "y"` inside a type and
`let x = "y"` at file scope (Swift), `const X = "y"` and one-level
`const Events = { X: "y" }` objects (TypeScript), `NSString *const X = @"y"` and
`#define X @"y"` (Objective-C). So `logEvent(Analytics.Event.PlayStart)` is read
as the constant's value. A bare name (`PlayStart`, from inside the class that
declares it) resolves only for a constant declared at file scope. A `var` (or a
TypeScript `let`) set to a string at file or type scope, and a Java `String`
field that is not `final`, are read the same way, as their initial value, even
though the code can change them before the call.

**Functions** declared to return a string, whose every `return` is a string
literal, resolve to that set of strings, and the call is checked once for each
of them: Swift `func f() -> String`, Kotlin `fun f(): String` or an `= "…"`
expression body, Java `String f()`, and TypeScript `function f(): string`. One
`return` of anything else makes the function dynamic. These are not read, so a
call to one is dynamic: a Swift function marked `async` or `throws`, a Kotlin
extension function (`fun Screen.f()`) or one with type parameters
(`fun <T> f()`), a Java method with a `throws` clause, Objective-C functions,
plain JavaScript functions, and TypeScript functions without a `: string` (or
`: String`) return type, class methods, arrow functions and function expressions.
A Swift computed property (`var x: String { "y" }`) is not read either: a
reference to one is dynamic.

Every scanned source file is read for enums, constants and functions too, so
`enums` is needed only for files outside `sources`. A file listed under `enums`
that also matches `exclude` is not read.

Still dynamic, though written from constants: `String.format` and `.format(…)`
templates, two constants joined with `+` (a concatenation is read only when one
of its parts is a string literal), a name picked by `if` or `when`, and a local
variable the name was stored in before the call.

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

A name the scanner cannot read at all (a variable, a parameter, a call of a
function it does not read) is dynamic. Such a call is counted but cannot be
matched to an event.

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
| `policy_violation` | error or warning | The call breaks a plan rule an installed extension adds, for example an organization's [plan governance](../enterprise/governance.md) policy in the Enterprise edition. `rule` names the rule (`naming.event`, `forbidden_field`, …). Community reports none. |
| `oversize_value` | warning | A name, event type, field or property was over the validator's size limits. The CLI sent the value as `null` (or dropped the key) instead of failing the batch. Raised by the CLI. |
| `no_verdict` | error | The CLI sent the item but the validator returned no verdict for it. Raised by the CLI, never by the server. It cannot be [suppressed](#suppress). |

Two more codes are about [`tripl-check-ignore` markers](#suppress), not about a
call. The CLI raises them and they never fail the run. They are the rule
(`ruleId`) of a SARIF result and the `check_name` of a Code Quality issue
(`tripl/invalid_suppression`). The text listing has a line for each instead
(`Suppression ignored:` and `Unused suppression:`), and the JSON document's
`suppressions` gives each marker's `problem` and `used` rather than a code:

| Code | Severity | Meaning |
|------|----------|---------|
| `invalid_suppression` | warning | A marker names no code, names `no_verdict`, or gives no reason. It suppresses nothing. |
| `unused_suppression` | info | A valid marker covers no finding with a code it lists in this run: the call was fixed, or the code is misspelled. |

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

## Suppress a finding {#suppress}

Sometimes a call is wrong on purpose: a debug-only event that never reaches
production, or a field the plan will gain next week. Put a
`tripl-check-ignore` comment at the call and give a reason:

```kotlin
// tripl-check-ignore[unknown_event] -- debug-only event, stripped from release builds (APP-12)
analytics.track("debug_tap", mapOf("screen" to "home"))
```

The finding no longer fails the run. JSON, SARIF, Code Quality and JUnit still
report it, marked as suppressed and with the reason; the text listing counts
it in its summary (`; 1 finding suppressed`) instead of listing it. The marker
reads the same in Swift, Objective-C, Kotlin, Java and TypeScript or
JavaScript:

```
tripl-check-ignore[<code>(,<code>)*] -- <reason>
```

- **Codes.** Each is a finding code (`unknown_event`), or a code and a field
  (`value_not_allowed:plan`) to waive it on that field only. Codes are not
  checked against a list: a misspelled one waives nothing and shows as unused.
- **Reason.** Everything after `--` to the end of the comment line (or to
  `*/`). It must contain a letter or a digit. JSON, SARIF, Code Quality and
  JUnit show it with the finding.
- **Comment forms.** Any `//`, `///`, `/* */` or `/** * */` comment, and JSX's
  `{/* */}`. The keyword must begin a comment line: right after `//`, `/*` or a
  block comment's leading `*`. A comment that mentions it mid-sentence
  (`// TODO tripl-check-ignore…`) is not a marker. Text inside a string, a
  template literal or a regular expression is never read.

**Which calls it covers.**

- Every matched call whose lines contain the marker's line, from the callee to
  the closing parenthesis, builder-chain links (`.label(…)`) included. A
  trailing comment covers the call on its line:

  ```swift
  tracker.track(name: "Cart", props: p)  // tripl-check-ignore[unknown_field:ab_variant] -- experiment field, plan entry pending
  ```

- When the marker is the first thing on its line, also every call that starts
  on the next line holding code. Blank lines and comment-only lines in between
  are skipped:

  ```objc
  /* tripl-check-ignore[deprecated_event] -- legacy screen, removed in 5.0 */
  [Tracker trackWithCategory:@"cart" action:@"old"];
  ```

  ```tsx
  {/* tripl-check-ignore[value_not_allowed:plan] -- value mapped by the ingestion proxy */}
  <Button onClick={() => track("buy", { plan: p })} />
  ```

A covered call is waived on every item it expands into (a name function that
returns several names, for example).

**A marker without a reason suppresses nothing.** Nor does one that names no
code or names `no_verdict`, which cannot be suppressed because nothing was
decided about the call. The finding fails the run as usual, so the required
reason is enforced by the check itself, and the marker is reported as
`invalid_suppression`:

```
Suppression ignored: app/src/Debug.kt:7 tripl-check-ignore needs "-- <reason>".
```

A malformed marker is never a usage error (exit 2) and never removes a report.

**An unused marker** covers no finding with a code it lists in this run. Two
markers on one call (one above it, one trailing) that list the same code are
both used. An unused marker is listed as
`unused_suppression` and never fails the run, with `--strict` either:

```
Unused suppression: app/src/Old.kt:3 tripl-check-ignore[unknown_event] matched no finding.
```

A suppression changes only whether the run fails. A result's `status` and the
summary counts stay the plan's verdict. Payload mode reads no source, so it has
no markers. One known limitation: in a JSX file, element text that looks like a
comment (`<p>// tripl-check-ignore…</p>`) is read as one.

## Gate on the change {#new-findings}

A repository that adopts `tripl check` late has legacy findings, and a plan
edit on main can turn a call nobody touched into an error. Neither should
block a merge request that did not cause it. With a gate, only the findings
that are **new in this change** fail the run:

```bash
tripl check --new-from-rev "$(git merge-base origin/main HEAD)"   # git: the change since that commit
git diff -M "$BASE" > change.diff          # or hg diff --git, svn diff --git, git format-patch
tripl check --new-from-patch change.diff   # needs no git
```

`--new-from-rev REF` asks git for the diff from the commit `REF` to the working
tree. The clone needs that one commit, not the history between
(`git fetch --no-tags --depth=1 origin REF`). `--new-from-patch FILE` reads the
same diff from a file, or from stdin with `-`, in **git's format only**: a
`diff --git` header on every file, with any of git's prefixes (`a/`/`b/`, the
mnemonic `c/`/`w/` and the like, or none) and any context size. Every VCS can
write one: `hg diff --git`, `svn diff --git`, `git format-patch` (a series, a
pull request's `.patch`, each change undone in turn), and for two plain trees
`git diff --no-index BASE_DIR .` run in the checkout (it exits 1 when they
differ). A plain `diff -u` or `diff -ruN`, Subversion's own format or `Only in`
lines are refused (exit 2): their paths and their missing sides cannot be read
reliably, and the gate does not guess. Pass `-M` to `git diff` so a renamed
file reads as a rename. Neither flag reads a CI variable.

**Where a patch's paths start.** At the repository root (the directory that
holds `.git`) when the check root is in a repository, as git writes them;
outside one, at the directory `tripl check` runs in, the way `git apply`
reads a patch. A path that leaves that directory (`../`), or that the patch
leaves in the tree and the checkout does not have, maps onto nothing (see
below).

**What is new.** The gate compares what each call **sends**, not its lines.
It rebuilds the tree as it was at the base by undoing the diff's hunks, scans
it with the same CLI and the same check configuration, and compares the items
of the two scans as multisets: an item is new or changed when the base has
fewer items that send the same thing (event type, name, field values, what
was only known at runtime, and which codes its valid `tripl-check-ignore`
markers waive) through the same function, in any file. The function is the
name the call ends in (or the Objective-C selector), not the receiver as
written, so dropping `self.` or `this.`, renaming the receiver or importing
a qualified name calls the same thing. The name of a placeholder in an
interpolated value is not part of it either (`promo_\(sheetId)_opened` and
`promo_\(id)_opened` are the same), while its text and the number and place
of its holes are. Nor is the order of named arguments or object keys, nor
the file: a call moved to another file sends what it sent. When the tree has more copies than the base, the ones in files the
change left alone are the old ones. The base is read by the scan's own rules:
what `exclude:` prunes, the 2 MB size limit (on the base's own bytes, line
ends included) and markers apply to it as they do to the checkout, and a
call in a file today's scan does not read (too large, unreadable) is not
compared: a copy of it pasted elsewhere is new. So:

- **new in the change:** a new call; an edited call; a copy of a call pasted
  anywhere; a call whose values changed through a constant in another file
  or an `enums:` file the change edited, or added (a second constant of the
  same name makes the value unknown, and that is a change too); a call whose
  marker the change removed, broke (no reason, a misspelled code) or added
  (its other findings then count as well); a pasted copy of a marked call
  that carries no marker; in `--new-from-rev`, every call in a file git does
  not track (generated code, a file not added yet), and every call that reads
  a constant from one;
- **not new:** a legacy finding at a call the change left alone; a call that
  was only moved, to another file too (a function pulled out into a new
  file, a file renamed and rewritten past git's rename detection), or
  reformatted, or whose named arguments were reordered, or whose marker's
  reason was reworded; a call deleted in one file and the same call added in
  another (the same thing is sent); a finding that appears only because
  `.tripl/check.yml` changed (both scans read today's configuration).

When the diff touches no file the scan reads (under `sources:` or `enums:`,
minus `exclude:`), nothing is scanned twice and nothing is new.

**A plan branch.** With `--branch`, the branch's own edits can fail a call
the change left alone, for example an event the branch archives. Those calls
are validated against main too, in one more batch: a finding main does not
have, or has at a lower severity, was caused by the branch, and it is new
(`cause: "plan_branch"` in JSON).

**What it changes.** The exit code, the text listing (only what is new or
fails, with a `Gate:` line counting the rest; a marker problem is listed only
in a file the change touched) and the gate fields of [JSON](#output) and SARIF
(`properties.inChange` on each result, `properties.gate` on the run, and no
`baselineState`). Code Quality and JUnit list every finding with and without
a gate, because GitLab works out what a merge request adds by comparing them
with the default branch's reports. So under a gate JUnit can show more failed
cases than the exit code counts. A result's `status` stays the plan's
verdict, and a [suppressed](#suppress) finding never fails, new or not.

```
Gate: changes since 1a2b3c4d5e6f (5 files, 3 call sites new or changed). 1 call site fails the run; 41 errors and 12 warnings are not new in this change.
Plan branch checkout-v2: 0 findings caused by the branch (2 call sites also checked against main).
```

**Edge cases.**

The gate **fails closed**: what it cannot rebuild counts as changed, never
as untouched, and a `Gate:` line (and the JSON `gate`) says so.

- A file whose diff does not fit it, a binary one, or one a patch deletes
  that is still there cannot be rebuilt: every call in it counts as changed
  (`Gate: the diff cannot rebuild … as it was`, JSON `unreconstructable`).
  A patch whose hunks do not fit a file it names outside the scan is not a
  patch of this checkout: every call counts as changed. (A file git's own
  diff deletes that is still on disk, untracked by the change, is compared
  with its base like any other.)
- A file the diff deletes whose base cannot be rebuilt may have defined
  constants other calls read: every call counts as changed.
- A scanned file that is a symlink changes with the file it points at,
  wherever that file sits, under `exclude:` or outside `sources:`. When the
  change moves any symlink, every scanned file read through a link counts as
  changed, because where a chain of links led at the base is not known.
- A submodule the change moves (a new commit, or tracked edits in it) counts
  as changed as a whole, every call in it (it is listed with a trailing `/`):
  the diff names its commit, not its files. An untracked file in it does not
  move it.
- A patch that names a file this checkout does not have (a patch of another
  tree, or with paths that start somewhere else) makes **every** call count as
  changed (`Gate: the diff names …, which this checkout does not have`, JSON
  `unmapped`).
- Line ends are compared with CRLF and LF read alike, so a checkout with
  `core.autocrlf` or `eol=crlf` fits the LF diff git prints.
- With `--new-from-rev`, a file the scan reads that git does not track, ignored
  or not, has no base: every call in it is new, and so is every call that
  reads a constant from it (`Gate: git does not track …`, JSON `untracked`).
  Keep generated sources out with `exclude:`, or accept that their calls
  always count.
- A `--new-from-patch` diff is all the gate knows: it must cover every change
  between the base and the checkout. A file generated in the checkout and not
  in the base, or not in the patch, is read as unchanged; put generated
  sources in `exclude:`, or generate them in the base as well.
- An empty `REF` (`--new-from-rev ""`) gates nothing and prints
  `--new-from-rev is empty: every finding counts.` on stderr, so one job line
  can serve merge request pipelines, where the base variable is set, and the
  default branch, where it is empty. An empty patch file is a change with
  nothing new.
- `no_verdict` fails the run whatever the gate says: nothing was decided.
- `--new-from-rev` and `--new-from-patch` are mutually exclusive, and both
  are refused with `--list` and `--payloads` (a captured event has no source
  to compare).

**Known limits.** The gate reads the change from git's diff, so what git
leaves out of the diff it does not see:

- A file git is told not to look at (`git update-index --assume-unchanged`
  or `--skip-worktree`) is not in the diff: an edit to it reads as unchanged
  under `--new-from-rev`.
- A clean filter in `.gitattributes` (`filter=…`, other than git-lfs) diffs
  what the filter makes of the file, not the file: an edit the filter hides
  reads as unchanged.

**When git cannot answer** the run stops with exit 2 before any request: git
is not installed (use the `ghcr.io/tripl-io/tripl-cli` image, which ships it,
or `--new-from-patch`), the directory is not a git checkout, the commit is not
in the clone (the message says how to fetch it: by SHA, as `origin/<branch>` for a branch, or with more history for a name like `HEAD^1`), git took longer than
60 seconds, or git refuses the repository because another user owns it
("detected dubious ownership", typical for a checkout mounted into a container
running as another uid). The CLI never works around that last one: the
message names the command that marks the checkout safe,
`git config --global --add safe.directory "<dir>"`, or run the container as the
checkout's owner (`docker run -u "$(id -u):$(id -g)" …`).

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

## List what the scan reads {#list}

`tripl check --list` scans your code exactly as `tripl check` does and shows
what it found. It needs no URL and no API key, and it sends nothing. Use it to
write `.tripl/check.yml`: run it after each change to the file.

```bash
tripl check --list
```

It prints three things:

- **Every call the config matches**, with what the scan read from it: the
  event name, each field and the properties, named as a call spec names them
  (a field called `name` is `field:name`). `?` is a value the scan did not
  read. Under such a call, an indented line says why, and `not mapped:` names
  the arguments the call spec maps to no target. A `?` inside `{…}` is a
  dictionary entry the scan did not read. It gets no line: its reason is only
  in the JSON, under the dictionary's `entries`
  (`read.properties.entries.<key>.reason`).
- **The functions that forward their own parameter** into a tracking call.
- **The calls that look like tracking** but match no call spec.

The summary counts the calls: how many are fully read, how many are not sent
(no name or field read, so `tripl check` counts them as `dynamic`), and how
many are sent with a value not read. It counts the top-level values only (the
name, each field, the properties as a whole), so a call whose only `?` is a
dictionary entry is fully read. `--json` prints the same as one document,
the [`check --list` document](../run/cli.md#check---list-document). The CLI
page has [an example](../run/cli.md#tripl-check-list).

`--list` exits 0 whenever the scan ran, even when the reader closed the pipe
early (`| head`), and 2 for a config error. It refuses the flags it has no use
for: `--report`, `--branch`, `--strict`, `--allow-empty`, and a `--format`
other than `text` or `json`.

### Why a value is not read {#list-reasons}

Each value the scan did not read gets one reason. Some reasons point at your
config. For the others there is nothing to configure: the scan does not read
that kind of value yet.

| Reason | What it is | What to do |
|--------|------------|------------|
| `parameter` | A parameter of the function the call sits in. The value comes from the function's callers. | Add a call spec for the function, so its callers are read (see [Functions that forward their parameter](#list-forwarders)). |
| `parameter_property` | A property of such a parameter, like `action.event`. The value is set where that object is built. | Configure the call that builds the object (often a constructor), not this function's callers. |
| `unknown_constant` | A constant or enum case the scan could not resolve. A name that starts with an upper-case letter, or with `k` and one (`kFIREventSelectContent`), reads as a constant. A bare one that names a parameter of the function the call sits in reads `parameter` instead; a Swift shorthand such as `.home` never does. A local of that name does not make it `local`: the scan does not track blocks, and the local may be in one that closed before the call. | Add its file to `enums:` or `sources:`. When several constants share the name, name its type more fully. An enum case needs a string raw value. When a local of that name is in scope at the call, there is nothing to configure. |
| `not_passed` | The call passes no name and no field value the scan can read: no argument or object key the call spec maps to one, only null ones, or an object argument that is not a literal. It is always reported on `name`. | Map the arguments that carry the name and the fields, in `positional:` or `args:`; the `not mapped:` line names the arguments no target takes. When they are mapped but passed as `null`, there is nothing to configure. When the reason line names an object argument that is not a literal (`read.object_argument` in the JSON), configure the call that builds the object. |
| `wrong_shape` | A value of the wrong shape for its target, such as properties given as text. | Check which argument goes to `properties`. |
| `local` | A local variable of the function, declared before the call. | Nothing to configure: the scan does not follow locals yet. |
| `format` | A printf-style format, such as `"promo_%s_shown".format(id)`. The reason shows the template when the scan read it. A call binds tighter than an operator, so `prefix + y.format(z)` is a `concatenation` and `name ?: y.format(z)` a `conditional`. `[NSString stringWithFormat:…]` is a format in Objective-C only: in Swift and TypeScript `[…]` is an array or dictionary literal, never a format. | Nothing to configure: the scan does not fill in formats yet. |
| `concatenation` | Values joined with `+`, with no string literal among them, or with a part that is a dictionary or a choice between values (a function that returns one of several literals). | Nothing to configure: the scan does not join these yet. |
| `conditional` | A choice between values: `if`, `when`, `switch`, a ternary, or a `?:` or `??` fallback. | Nothing to configure. |
| `function` | A call of a function the scan does not read. It reads one whose every `return` is a string literal, declared like `fun f(): String` or `fun f() = "…"` (Kotlin), `func f() -> String` (Swift), `String f()` (Java) or `function f(): string` (TypeScript). [Enum sources](#enum-sources) lists those it does not read, such as a TypeScript method or arrow function. | When the function is declared that way in a file outside `sources:`, add the file to `enums:` or `sources:`. Otherwise nothing to configure. |
| `expression` | Any other expression: a chained call, an operator, a subscript, a lambda. | Nothing to configure. |
| `variable` | A variable the scan does not follow: a property, a lambda parameter, a captured variable. | Nothing to configure. |

The function and the line a `parameter`, `parameter_property` or `local` reason
names come from reading the source, not from compiling it. They are a hint:
nothing `tripl check` sends or decides depends on them.

The scan knows a function by its declaration: a Kotlin `fun`, a Swift `func` or
`init`, a Java or C function, and a TypeScript `function`, method or named
arrow (`const track = (event: string) => …`). A TypeScript return type may be
an object or a function type (`Promise<{ ok: boolean }>`, `() => void`). A
lambda, a closure or an Objective-C method is not read as a function: a value
in one is explained from the named function around it, if there is one.

An expression body (`= …`, `=> …`) ends with its line, unless a bracket is
still open, the line ends in an operator, or the next line starts with `.`,
`?.`, `&&` or `||`. An operator here is one of `+`, `-`, `*`, `/`, `%`, `&`,
`|`, `^`, `&&`, `||`, `??`, `==`, `!=`, `===`, `!==`, `<=`, `>=`, `=`, `=>`,
`:`, `.` and `?.`; `<`, `>` and a postfix `++` or `--` are none. In Kotlin a
line that ends in `,`, `else` or the `)` of a brace-less `if (…)` goes on too,
and so does a next line that starts with `?:`, `else`, `catch` or `finally`,
but not one that starts with `+` or `-`: Kotlin reads that as a new
statement. In TypeScript a line that ends in `?` goes on too, and so does a
next line that starts with `?` or an operator other than `=` and `=>`; a `,`
at the body's level ends the body. In both languages, so does a `;`.

### Functions that forward their parameter {#list-forwarders}

A wrapper such as `fun track(event: String) { analytics.logEvent(event) }`
hides the event name from the scan. At the matched call, the name is only the
parameter `event`; it is written where `track` is called. `--list` names each
such function, the parameters it forwards, and how many calls of it it found
in the same file and elsewhere, matched or not. Add a call spec for the
function, and its calls are read:

```yaml
event_types:
  event:
    calls:
      - function: "analytics.logEvent"
        positional: [name, properties]
      - function: "track"          # the wrapper --list named
        positional: [name]
```

The calls are counted by name, in the function's own file and elsewhere,
leaving out those in its own body: a call of that name there is the tracking
call it forwards to, or the function calling itself. By name alone, so a call
of another function with the same name counts too, a tracking call named like
it included. A Swift `init` or a TypeScript `constructor` is listed under its
type's name, which is what its callers write (`ScreenView(screen: name)`,
`new PageView(path)`), though the reason line under the call, and
`declared.function` in the JSON, still name it `init` or `constructor`. One in
an anonymous class is not listed.

### Calls that look like tracking {#list-tracking-like}

`--list` also shows the calls that no call spec matches but that look like
tracking, grouped by method: the callee's last segment, or an Objective-C
message's selector. Each group has a signal, strongest first:

- `constant`: an argument is an event-name constant, a constant whose path
  names an event (`wrap(Events.Name.Open)`). These groups come first, and the
  text shows all of them.
- `name`: the method's name says so. It contains `event`, has a word that
  starts with `track`, or is `capture`, `screen`, `screenView`,
  `logScreenView` or `logPurchase`.
- `receiver`: a tracking vendor's receiver (`analytics`, `tracker`,
  `amplitude`, `firebase` and the like) calls a tracking verb such as `log`,
  `track` or `send`.

These methods take event constants too, so they never give the `constant`
signal (their name or receiver can still give another one):

- the formats `format`, `formatted`, `stringWithFormat` and
  `localizedStringWithFormat`;
- the collection builders `listOf`, `setOf`, `arrayOf`, `mutableListOf`,
  `mutableSetOf`, `hashSetOf`, `linkedSetOf`, `sortedSetOf`, `arrayListOf`,
  `listOfNotNull`, `setOfNotNull`, `mapOf`, `mutableMapOf`, `hashMapOf`, `of`
  (`List.of`, `Map.of`, `EnumSet.of`) and `asList`, and `add`, `addAll`,
  `push`, `equals` and every method that starts with `put`;
- a test's assertions and matchers, and React's hooks: a method that starts
  with `expect`, `assert`, `XCTAssert`, `should`, `to`, `is`, `use`, `eq` or
  `equal`, then an upper-case letter or nothing (`assertEquals`, `toBe`,
  `isEqualTo`, `shouldBe`, `eq`, `equalTo`, `useState`).

Event plumbing is left out. A method is plumbing when it:

- is `addEventListener`, `removeEventListener`, `dispatchEvent`,
  `preventDefault`, `stopPropagation`, `createEvent` or `initEvent`, or ends
  in `listener`, in any case;
- is a handler: it starts with `on` and an upper-case letter (`onClick`), or
  with `handle` in any case;
- contains an input event: `TouchEvent`, `KeyEvent`, `PointerEvent`,
  `MotionEvent`, `DragEvent`, `InputEvent` or `AccessibilityEvent`;
- is a UI one-shot event holder: it ends in `LiveEvent`, or is
  `EventObserver`;
- is a DOM event type called with no receiver, with or without `new`:
  `Event`, `CustomEvent`, `UIEvent`, `MouseEvent`, `KeyboardEvent`,
  `FocusEvent`, `WheelEvent`, `InputEvent`, `PointerEvent`, `TouchEvent`,
  `DragEvent`, `CompositionEvent`, `ClipboardEvent`, `AnimationEvent`,
  `TransitionEvent`, `MessageEvent`, `ErrorEvent`, `ProgressEvent`,
  `PopStateEvent`, `HashChangeEvent`, `StorageEvent`, `SubmitEvent`,
  `BeforeUnloadEvent` or `PageTransitionEvent`. A bare `Event(…)` is also
  Android's one-shot event holder. `Analytics.Event(…)` has a receiver, so it
  can still be listed.

Except where they say "in any case", these two lists match a method's name as
written. For an Objective-C message, that name is its selector's first part.

A call in a matched call's arguments, or in those of a chained method its call
spec maps under `chain:` (to a target or to `null`), or one that wraps a
matched call, is left out too; a call in another chained method, such as
`.then(…)`, is listed. A declaration with no body, which reads like a call, is
left out as well: an interface or abstract method, a protocol requirement, an
overload, a C prototype in a header. The scan tells one from a call by a
declaring keyword (`fun`, `func`, `init`, `function`), a return type (the end
of a preprocessor line, as in `#endif`, is none), a typed parameter or a
modifier, so a TypeScript member with none of these (`track(name, props);`) is
still listed. The text shows the 10 largest groups besides the `constant`
ones; `--json` lists them all.

### Labels read as fields {#list-default-labels}

A call spec without `args:` maps every labelled argument to the field of the
same name. That is often right, but a label like `key` or `exclude` then reads
as a plan field. `--list` notes it, per event type:

```text
event: fields key, exclude are read from the labels key and exclude because the call spec has no args:; map them in args: (null drops one).
```

List the labels in `args:`, and map the ones that are not fields to `null`.

### No config yet {#list-discovery}

With no `.tripl/check.yml` found and no `--check-config`, `--list` scans the
whole git repository it runs in, every file in a supported language. It skips
the built-in [excludes](#config-keys) and every hidden directory (`.venv`,
`.gradle`, `.next` and the like), which hold installed or generated code, not
your app's. Nothing can match, so it lists the calls that look like tracking: a
place to start the config. It needs a git checkout; outside one, it is exit 2. It reads at most
20000 source files. When it stops there, it says so: write the config with
`sources:` to scan the rest.

## Output formats {#output}

`--format` picks what goes to stdout, and `--report FORMAT=PATH` writes the
same result to a file. Repeat `--report` for every file your CI reads: one run,
one validation, the same verdicts in every file.

| Format | What it is |
|--------|------------|
| `text` (the default) | Human-readable: every item that is not a clean pass, with its location and findings, then a summary. A [suppressed](#suppress) finding is counted in the summary (`; 1 finding suppressed`), not listed, and a marker that suppresses nothing or matched nothing gets a line after it. |
| `json` (or `--json`) | One JSON document with a row for **every** item, passing ones included. See the [`check` document](../run/cli.md#check-document). |
| `sarif` | A [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) log, for GitHub code scanning and other SARIF viewers. |
| `codequality` | A [GitLab Code Quality](https://docs.gitlab.com/ci/testing/code_quality/) report, for the merge request's Code Quality report on every GitLab tier. |
| `junit` | JUnit XML, for GitLab's test summary and the test reports of most other CI systems. |

```bash
tripl check --report codequality=gl-code-quality-report.json --report junit=tripl-check.xml
```

**SARIF.** Each finding is one result and its code is the rule (`ruleId`).
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
useful for the static mode. A [suppressed](#suppress) finding stays a result,
with `suppressions: [{"kind": "inSource", "status": "accepted",
"justification": <reason>, "location": <the marker>}]`, which code scanning
shows as dismissed. A marker that suppresses nothing is a result of its own,
rule `invalid_suppression` at `level: "warning"`, and an unused one rule
`unused_suppression` at `level: "note"`, both at the marker's line.

**Code Quality.** One issue per finding. `check_name` is `tripl/` and the code
(`tripl/unknown_event`); `severity` is `major` for an error, `minor` for a
warning and `info` for an info finding; `location` names the file and the
call's lines. A [suppressed](#suppress) finding stays in the report with the
same fingerprint, at `severity: info` and with its description starting
`Suppressed: <reason> — `, so GitLab shows it as neither new nor resolved. A
marker that suppresses nothing is a `tripl/invalid_suppression` issue
(`minor`), an unused one a `tripl/unused_suppression` issue (`info`).

**JUnit.** One test case per call site (or captured event), passing ones
included. A case is named after the call (its event type, name, field values
and callee) and never after its line, so it keeps its history in the CI system
when code above it moves; identical calls in one file are numbered (`#2`). Its
class is the file. It fails exactly when its call fails the run without a
[gate](#new-findings): an error, or a warning with `--strict`. It ignores the
gate, as Code Quality does. A call where nothing was readable is skipped, and with
`--strict` it is a warning, so it fails. A warning without `--strict` passes,
with its findings as the case's output. A [suppressed](#suppress) finding fails
no case: it is listed in the case's output tagged `[suppressed: <reason>]`.

**Paths.** In Code Quality and JUnit, paths are relative to the repository root
(the nearest directory that holds `.git`), the way a CI system resolves them
against its checkout, even when `.tripl/` sits in a subdirectory of a monorepo.
The text, JSON and SARIF formats keep paths relative to the scan root.

**Fingerprints.** Every finding has a fingerprint, the same value in JSON
(`findings[].fingerprint`), SARIF (`partialFingerprints["triplCheck/v2"]`) and
Code Quality (`fingerprint`). It is built from what the call is (its file,
callee, event type, name and field values, and the finding's code and field),
never from its line and never from what the plan says. So a CI system comparing
two runs, as GitLab's Code Quality report does, sees the same finding after an
edit above it moves it down the file, and a new one once the call itself
changes. Identical calls in one file are told apart by their order.
Suppressing a finding leaves its fingerprint as it was. A reported marker has a
fingerprint of its own, built from its file, its problem and its text, never
its line.

**JSON.** `status` is the plan's verdict. When the scan read a
`tripl-check-ignore` marker, the document also says what the markers did:
`results[].fails` and `results[].findings[].fails` say what fails the run,
`findings[].suppression` names the marker that waived a finding, `summary`
gains `suppressed` and `failing`, and `suppressions` lists every marker. With
no marker in the scanned files, none of these keys is present. Under a
[gate](#new-findings), and only then, `gate` says what was compared, every row
and finding gains `in_change` and `fails`, a finding `cause`, and `exit_code`
is the gated one. See the [`check` document](../run/cli.md#check-document).

**A run that could not finish writes no report**: a configuration or usage
error (exit 2), an instance that refused a request (exit 1) or was unavailable
(exit 4), or a crash. Nothing goes to stdout or to any `--report` file, and a
file an earlier run left at a `--report` path is removed, so it cannot pass for
this run's. That includes connection settings that do not resolve, such as a
`TRIPL_BASE_URL` without `https://` or a `--config` file that does not exist.
Only a regular file is ever replaced or removed: a `--report` path that is or
names a directory (`reports/`), a link or a special file is refused (exit 2). A
command line the parser rejects outright, such as an unknown flag, leaves every
file alone, because it is refused before the command starts. A report that cannot be
written fails the run (exit 1) before anything is printed.

Human text and progress messages go to stderr when stdout carries `json`,
`sarif`, `codequality` or `junit`, so you can redirect stdout to a file.

## Exit codes {#exit-codes}

| Code | Meaning |
|------|---------|
| **0** | No errors, or only errors a `tripl-check-ignore` marker [suppresses](#suppress) or, under a [gate](#new-findings), that are not new in the change. Warnings are allowed unless you pass `--strict`. |
| **1** | At least one error, or at least one warning with `--strict`, that no `tripl-check-ignore` marker suppresses and that, under a gate, is new in the change. A malformed or unused marker never fails the run on its own. Also: the instance refused a request (an invalid or expired key, a project the key cannot see, a redirect). Then no document or report is written. |
| **2** | A config or usage error: no config file in static mode, invalid YAML, an unknown key or preset, a call without exactly one of `function` / `pattern` / `objc_selector`, a configuration that matches no call, a `--payloads` file that is not valid JSON, holds no events or holds an event with neither `event_type` nor `name`, no project, `--json` together with another `--format`, a `--report` that is not `FORMAT=PATH`, names one file twice or names a directory, a link or a special file, or a `--branch` that matches nothing. Under a [gate](#new-findings): a gate flag with `--list` or `--payloads`, a `REF` that starts with `-`, no git, not a git checkout, a commit that is not in the clone, a repository git refuses as owned by another user, a git call over 60 seconds, or a patch that cannot be read, holds no diff or is not in git's format; all before any request. |
| **4** | The instance was unavailable: it could not be reached, the request timed out, or it answered `429` or a `5xx`. Nothing was checked, and no document or report is written. Let it pass in CI when an outage of tripl should not block merges. |

A configuration that matches no call checks nothing, and a CI check must not
report that as a pass, so it is exit 2: usually a `sources:` glob after a
directory moved, or a call spec after a wrapper was renamed. `--allow-empty`
accepts it, for a repository with no tracking code yet, and also accepts a
`--payloads` file with no events.

`tripl check --list` checks nothing, so it never fails because nothing
matched: it exits 0 whenever the scan ran, and 2 for a config error or a flag
it refuses (see [List what the scan reads](#list)).

`--strict` makes warnings fail the run, reports a `dynamic_value` finding for
every value the scanner could not read, and lists the calls where nothing was
readable (status `dynamic`), counting them as warnings. Use it in a gate you
watch, not in an unattended job, where a new dynamic value would turn the job
red.

`--branch` checks against a plan branch instead of main. Use it for a pull
request that ships code for events that are still on a plan branch under
review.

## In CI {#ci}

[Check tracking code in CI](../how-to/check-tracking-in-ci.md) walks through
it: a read-only key for a bot user, the job for
[GitLab CI/CD](../how-to/check-tracking-in-ci.md#gitlab),
[GitHub Actions](../how-to/check-tracking-in-ci.md#github-actions) and
[other CI systems](../how-to/check-tracking-in-ci.md#other-ci), and how to make
the check block merges. In short:

- Pin the CLI to the version your instance runs (the image
  `ghcr.io/tripl-io/tripl-cli:X.Y.Z`, `pip install tripl==X.Y.Z` or
  `uvx tripl@X.Y.Z`): the two are released together.
- A read-only key (`tk_r_`) is enough, because the check only reads the plan.
  If you already keep a write key in secrets for
  [`tripl annotate`](../run/cli.md#in-a-github-actions-workflow), do not reuse
  it here. Mint a read key and scope it to the project.
- Write the report your CI shows: `codequality` and `junit` for GitLab, `sarif`
  for GitHub code scanning, `junit` for most others.
- To fail a merge request only on what it adds, [gate](#new-findings) the run
  on its base commit with `--new-from-rev`; the how-to has the line for each
  CI.
- To check against a plan branch named after the merge request's branch, add
  `${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME:+--branch "$CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"}`
  (GitLab; it adds nothing outside a merge request pipeline), or set
  `HEAD_REF: ${{ github.head_ref }}` in the step's `env` and add
  `${HEAD_REF:+--branch "$HEAD_REF"}` (GitHub; never paste `${{ … }}` into the
  script itself, where a branch name would run as shell). The check exits 2 when
  no such plan branch exists, so use this only if your team follows that naming
  convention.

To check captured events instead, for example from a UI test run that writes
what the app sent to `build/events.ndjson`:

```bash
tripl check --payloads build/events.ndjson --report junit=tripl-events.xml
```

## Related pages {#related}

- [`tripl check`](../run/cli.md#tripl-check) on the CLI page: every flag.
- [Check tracking code in CI](../how-to/check-tracking-in-ci.md): the jobs for
  GitLab, GitHub and other CI systems.
- [Agent API guide: plan validation](./agent-api-guide.md#plan-validation): the
  endpoint behind the check, for your own tools.
- [Properties & templates](../use/variables-and-templates.md): documented values
  and `${variable}` placeholders.
