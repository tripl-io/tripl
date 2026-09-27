---
title: Generate typed tracking code (tripl codegen)
---

# Generate typed tracking code: `tripl codegen` {#tripl-codegen}

[`tripl check`](./tripl-check.md) finds a misspelled event in a pull request.
Generated code keeps you from typing it in the first place. When the plan's
values are enum cases, a wrong category, an unknown screen or a missing
required value is a **compile error** in Xcode, Android Studio or `tsc`. The
plan stays the source of truth, and developers get autocompletion for every
value it allows.

`tripl codegen` reads the plan and writes Swift, Kotlin and TypeScript source
files into your repository. It is built around three decisions:

- **No function per event.** A plan with 400 events does not become 400
  functions. The generated API follows how each **event type** is tracked.
  Structured events keep your wrapper's call and only narrow its argument
  types. Screen views get typed screen enums. Named and self-describing events
  go through one `track` call that takes a typed value.
- **Your wrapper stays in charge.** The generated code never imports an SDK.
  It calls **your** tracking function (the *transport*). An event type with no
  transport goes through one shared seam, `TriplDestination`, that you
  implement once. Batching, consent, user ids and which SDK receives the event
  stay in your code.
- **Configured per event type.** The configuration lives next to the
  `tripl check` configuration in `.tripl/check.yml`. Each event type can pick
  its style, languages and transport, rename the generated types, and replace
  the built-in template with its own.

The command only reads the plan, so a read-only `tk_r_` key is enough. With
`--model FILE` it reads a saved export and needs no instance at all. For every
flag, see [`tripl codegen`](../run/cli.md#tripl-codegen) on the CLI page. To get
the plan as JSON Schema instead of code, see
[JSON Schema export](#json-schema-export).

## Styles {#styles}

Each event type gets one of four **styles**:

| Style | For event types like | What is generated |
|-------|----------------------|-------------------|
| `structured` | Category / action / label events, identified by a name rule such as `{category}:{action}:{label}` (Snowplow structured events, Google Analytics-style events). | An enum for every field whose plan values are all known, a `knownEvents` list of the planned combinations, and a function with **your wrapper's arguments**, typed with those enums. |
| `screen_view` | Screen views identified by a screen type and an optional id. | The same, with the enums named `ScreenType` and `ScreenId`. |
| `named` | Flat event names with a properties dictionary (Segment `track`, Amplitude `logEvent`). | One typed value per event and **one** `track` function. Swift: an `enum` with one case per event. Kotlin: a `sealed interface` with a `data class` or `object` per event. TypeScript: an interface from plan name to properties, and `track<K extends …Name>(name: K, props: …Props[K])`. |
| `self_describing` | Schema-based events, one schema per event (Snowplow self-describing events). | One type per schema with its data fields, and one `track` function that takes any of them. |

`style` is optional. Without it, the style comes from the event type's preset
(`segment_track` and `amplitude_log_event` give `named`, `snowplow_structured`
gives `structured`, `snowplow_screen_view` gives `screen_view`,
`snowplow_self_describing` gives `self_describing`). Without a preset, it comes
from the plan: `structured` when the event type has a name rule, `named` when
it does not.

In every style:

- The **raw value is always the exact plan string**. Only the identifier is
  derived from it (see [Identifier naming](#identifier-naming)).
- A value that is a `${variable}` placeholder contributes the variable's
  **allowed values**. An event can override them for itself, and then its own
  list is used. A variable with no allowed values (or an empty override) is
  free text, so the field stays a `String`.
- A field defined as an enum in the plan becomes an enum of its options.
- A required field is a non-optional parameter. An optional field is an
  optional parameter with a `nil` / `null` / `undefined` default. The
  `properties` parameter is the exception: it is **non-optional**, with an
  empty default (`[:]` / `emptyMap()` / `{}`), so a call can leave it out and
  your wrapper always receives a dictionary.
- Archived events are left out.

In the `named` and `self_describing` styles:

- A value the plan fixes for an event is filled in by the generated code and
  is not a parameter. On a boolean or number field it is sent typed (`true`,
  `4.5`); every other fixed value is sent as a string.
- A `${token}` in an event's name or in a fixed value becomes a parameter,
  typed by the variable's allowed values (`promo_sheet_${sheet_id}_shown` takes
  `sheetId: SheetId`). The generated code fills it into the name.
- Deprecated events are generated and marked deprecated
  (`@available(*, deprecated)`, `@Deprecated`, `@deprecated`), so existing
  calls compile with a warning. See [Deprecated events](#deprecated-events).

In the `structured` and `screen_view` styles, events only contribute their
values to the enums, so nothing is marked deprecated there.

## The configuration {#config}

Codegen reads the same [`.tripl/check.yml`](./tripl-check.md#config-file) as
`tripl check`. An event type opts in with a `codegen` block; one without it is
skipped. A top-level `codegen` block holds what every event type shares.

This is the configuration of the CLI's own test fixture
(`cli/tests/codegen/check.yml`). Every generated sample on this page is the
output of `tripl codegen` for it:

```yaml
project: demo
codegen:
  out: generated
  kotlin_package: com.example.tracking
event_types:
  se:
    calls:
      - function: "Analytics.shared.log"
        args: {category: category, action: action, label: label, properties: properties}
    codegen:
      style: structured
      transport:
        swift: "Analytics.shared.log"
        kotlin: {function: "Analytics.log", args: {category: category, action: action, label: label, properties: properties}}
        ts:
          function: "analytics.log"
          positional: [category, action, label, properties]
          import: "import { analytics } from './analytics';"
      type_names: {namespace: AppEvents}
  screen:
    preset: snowplow_screen_view
    codegen: {}
  legacy:
    calls: [{function: "Analytics.shared.logEvent", name_arg: 0, properties_arg: parameters}]
    codegen:
      style: named
      transport: {swift: "Analytics.shared.logEvent"}
  sd:
    preset: snowplow_self_describing
    codegen:
```

`screen` and `sd` opt in with every default. Their style comes from the preset,
they are generated for all three languages, and with no transport they go
through [`TriplDestination`](#destination).

### The top-level `codegen` block {#codegen-top-keys}

| Key | Meaning |
|-----|---------|
| `out` | Where the files go. A directory, which gets one subdirectory per language (`generated/swift`, `generated/kotlin`, `generated/ts`), or a map from language to directory (`{swift: DIR, kotlin: DIR, ts: DIR}`). Relative paths start from the directory that holds `.tripl/` (or `root`, when set). `tripl codegen --out` overrides it. With neither, a run is a configuration error (exit 2). |
| `languages` | The default `languages` for every event type. Default: `[swift, kotlin, ts]`. |
| `kotlin_package` | The `package` line of every Kotlin file, e.g. `com.example.tracking`. Without it, the Kotlin files have no `package` line. |
| `vars` | Optional. A flat map of names to **strings** that every template reads as `{{vars.NAME}}`, e.g. `{prefix: Acme}`. Quote a number or boolean. See [`vars`](#template-vars). |

### An event type's `codegen` block {#codegen-keys}

| Key | Meaning |
|-----|---------|
| `style` | Optional. `structured`, `screen_view`, `named` or `self_describing`. Default: from the preset, then from the plan (see [Styles](#styles)). |
| `languages` | Optional. Any of `swift`, `kotlin`, `ts`. Default: the top-level `languages`, else all three. `tripl codegen --lang` narrows this further for one run. |
| `transport` | Optional. Per language, the call the generated code forwards to. See [The transport](#transport). A language with no entry goes through [`TriplDestination`](#destination). |
| `type_names` | Optional. Renames generated types, taken verbatim. `namespace` is the enclosing type and the file name; `event` is the `named` / `self_describing` event type; `function` is the generated function; any **plan field name** or **`${token}` name** renames the enum generated for it. |
| `template` | Optional. Your own template: a map from language to path, or one path when `languages` has exactly one entry. Relative paths start from the directory that holds `.tripl/`. See [Custom templates](#custom-templates). |
| `vars` | Optional. Like the top-level `vars`, merged over them for this event type. |
| `files` | Optional. Extra files rendered from your own templates with the same context, written next to the event type's own file. See [`files`](#template-files). |
| `schema` | Optional, for `self_describing`. The schema URI sent with each event, where `{name}` is replaced by the event's identity. Default: the identity itself. When the URI is an Iglu URI, the type is named after the schema name, with a version suffix when the plan has several versions of it (`CheckoutStartedV1_0_0`). |

A `codegen` block with an unknown key, style or language, a template file that
does not exist, or a `type_names` value that is not an identifier in one of the
event type's languages is a configuration error (exit 2), like any other
mistake in the file. A type name must also not shadow one the language already
has (`String`, `Map`).

## Generated code, by style {#generated-code}

The samples below are the CLI's own test output: `tripl codegen` run on the
fixture plan `cli/tests/codegen/model.json` with the configuration above. They
are copied from `cli/tests/codegen/golden/`. The fixture plan has:

- `se` (name rule `{category}:{action}:{label}`), with the events
  `home:open:card`, `1st run:open:card`, the deprecated `default:open:card`,
  `checkout:${checkout_step}:promo_sheet` and `checkout:tap:${promo_id}`. The
  variable `checkout_step` allows `start` and `confirm`; `promo_id` has no
  allowed values.
- `screen` (name rule `{type}`), with the screen types `HomeView` (id `home`)
  and `Checkout Start` (id `checkout:start`).
- `legacy` (no name rule), with the fields `mode` (required string),
  `platform` (optional enum `ios`, `android`) and `is_first` (optional
  boolean), and the events `Home Screen View`, `trial_started` (fixes `mode`),
  `promo_sheet_${sheet_id}_shown` (fixes `mode` to `promo_${sheet_id}`), the
  deprecated `checkout:start` (fixes all three fields) and the archived
  `old_event`.
- `sd` (no name rule), with the fields `sku` (required string) and `price`
  (optional number), and three schemas: `checkout_started` 1-0-0 (deprecated)
  and 2-0-0, and `promo_sheet_viewed` 1-0-0, which fixes `sku` to `none` and
  `price` to `4.5`.

### The header {#header}

Every file starts with the same header. It names the project, the branch and
the plan's **content hash** (the export's `plan_hash`). It carries no
timestamp and no revision id, so output is byte-for-byte deterministic: a
re-run against an unchanged plan changes nothing, and neither does a new
revision that changes nothing these files depend on.

```swift
// Generated by tripl codegen — do not edit.
// Plan: project demo, branch main, plan 5e0d4c3b2a19.
// Event type: se (structured). Regenerate with `tripl codegen`.
```

The shared transport files have the first two lines and then
``// Regenerate with `tripl codegen`.`` When an export has no `plan_hash`, the
header carries a sha256 over the generated files instead. Enum cases are
sorted by raw value and events by identity.

### Structured {#style-structured}

The function keeps **your wrapper's arguments**: they come from the
`transport` mapping (here the `calls` entry for `Analytics.shared.log`), so
`value`, which the wrapper does not take, is not a parameter. The function is
named after the wrapper's method (`log`); without a transport it is `track`.
Only the argument types change, from `String` to the plan's enums. `label`
stays a `String` because `${promo_id}` has no allowed values, and
`knownEvents` lists only the events whose values are all literal.

Swift (`AppEvents.swift`):

```swift
/// Typed values for the `se` event type (structured): one enum per
/// plan field, and the wrapper's own call with its arguments narrowed to them.
public enum AppEvents {
    public enum Category: String, CaseIterable, Sendable {
        case _1stRun = "1st run"
        case checkout = "checkout"
        case default_ = "default"
        case home = "home"
    }

    public enum Action: String, CaseIterable, Sendable {
        case confirm = "confirm"
        case open = "open"
        case start = "start"
        case tap = "tap"
    }

    public struct KnownEvent: Sendable {
        public let identity: String
        public let category: Category
        public let action: Action
        public let label: String
    }

    /// Every planned combination, checked by the compiler against the enums above.
    public static let knownEvents: [KnownEvent] = [
        KnownEvent(identity: "1st run:open:card", category: Category._1stRun, action: Action.open, label: "card"),
        KnownEvent(identity: "default:open:card", category: Category.default_, action: Action.open, label: "card"),
        KnownEvent(identity: "home:open:card", category: Category.home, action: Action.open, label: "card"),
    ]

    public static func log(category: Category, action: Action, label: String? = nil, properties: [String: Any] = [:]) {
        Analytics.shared.log(category: category.rawValue, action: action.rawValue, label: label, properties: properties)
    }
}
```

Kotlin (`AppEvents.kt`). The transport's `args` make every argument a named
one:

```kotlin
package com.example.tracking

/**
 * Typed values for the `se` event type (structured): one enum per
 * plan field, and the wrapper's own call with its arguments narrowed to them.
 */
object AppEvents {
    enum class Category(val value: String) {
        _1ST_RUN("1st run"),
        CHECKOUT("checkout"),
        DEFAULT("default"),
        HOME("home");
    }

    enum class Action(val value: String) {
        CONFIRM("confirm"),
        OPEN("open"),
        START("start"),
        TAP("tap");
    }

    data class KnownEvent(
        val identity: String,
        val category: Category,
        val action: Action,
        val label: String,
    )

    /** Every planned combination, checked by the compiler against the enums above. */
    val knownEvents: List<KnownEvent> = listOf(
        KnownEvent(identity = "1st run:open:card", category = Category._1ST_RUN, action = Action.OPEN, label = "card"),
        KnownEvent(identity = "default:open:card", category = Category.DEFAULT, action = Action.OPEN, label = "card"),
        KnownEvent(identity = "home:open:card", category = Category.HOME, action = Action.OPEN, label = "card"),
    )

    fun log(category: Category, action: Action, label: String? = null, properties: Map<String, Any?> = emptyMap()) {
        Analytics.log(category = category.value, action = action.value, label = label, properties = properties)
    }
}
```

TypeScript (`appEvents.ts`). Enums are `as const` objects with a union type of
the same name, so both `Category.checkout` and the literal `'checkout'`
type-check, and `'chekout'` does not. The transport is positional and brings
its own `import` line:

```ts
import { analytics } from './analytics';

export const Category = {
  _1stRun: '1st run',
  checkout: 'checkout',
  default_: 'default',
  home: 'home',
} as const;
export type Category = (typeof Category)[keyof typeof Category];

export const Action = {
  confirm: 'confirm',
  open: 'open',
  start: 'start',
  tap: 'tap',
} as const;
export type Action = (typeof Action)[keyof typeof Action];

export interface KnownEvent {
  readonly identity: string;
  readonly category: Category;
  readonly action: Action;
  readonly label: string;
}

/** Every planned combination, checked by the compiler against the types above. */
export const knownEvents: readonly KnownEvent[] = [
  { identity: '1st run:open:card', category: Category._1stRun, action: Action.open, label: 'card' },
  { identity: 'default:open:card', category: Category.default_, action: Action.open, label: 'card' },
  { identity: 'home:open:card', category: Category.home, action: Action.open, label: 'card' },
];

/**
 * The `se` event type (structured): the wrapper's own call, with its
 * arguments narrowed to the plan's values.
 */
export function log(category: Category, action: Action, label?: string, properties: Readonly<Record<string, unknown>> = {}): void {
  analytics.log(category, action, label, properties);
}
```

### Screen view {#style-screen-view}

`screen` has no transport, so it forwards to
[`TriplTracking.send` / `triplSend`](#destination), with the screen type as
the event's name.

Swift (`ScreenTracking.swift`):

```swift
/// Typed screen views for the `screen` event type (screen_view): the plan's
/// screen types (and ids, where it has them), and the wrapper's call narrowed to them.
public enum ScreenTracking {
    public enum ScreenType: String, CaseIterable, Sendable {
        case checkoutStart = "Checkout Start"
        case homeView = "HomeView"
    }

    public enum ScreenId: String, CaseIterable, Sendable {
        case checkoutStart = "checkout:start"
        case home = "home"
    }

    public struct KnownEvent: Sendable {
        public let identity: String
        public let type: ScreenType
    }

    /// Every planned screen view, checked by the compiler against the enums above.
    public static let knownEvents: [KnownEvent] = [
        KnownEvent(identity: "Checkout Start", type: ScreenType.checkoutStart),
        KnownEvent(identity: "HomeView", type: ScreenType.homeView),
    ]

    public static func track(type: ScreenType, id: ScreenId? = nil, properties: [String: Any] = [:]) {
        TriplTracking.send(eventType: "screen", name: type.rawValue, fields: ["type": type.rawValue, "id": id?.rawValue], properties: properties)
    }
}
```

Kotlin (`ScreenTracking.kt`):

```kotlin
package com.example.tracking

/**
 * Typed screen views for the `screen` event type (screen_view): the plan's
 * screen types (and ids, where it has them), and the wrapper's call narrowed to them.
 */
object ScreenTracking {
    enum class ScreenType(val value: String) {
        CHECKOUT_START("Checkout Start"),
        HOME_VIEW("HomeView");
    }

    enum class ScreenId(val value: String) {
        CHECKOUT_START("checkout:start"),
        HOME("home");
    }

    data class KnownEvent(
        val identity: String,
        val type: ScreenType,
    )

    /** Every planned screen view, checked by the compiler against the enums above. */
    val knownEvents: List<KnownEvent> = listOf(
        KnownEvent(identity = "Checkout Start", type = ScreenType.CHECKOUT_START),
        KnownEvent(identity = "HomeView", type = ScreenType.HOME_VIEW),
    )

    fun track(type: ScreenType, id: ScreenId? = null, properties: Map<String, Any?> = emptyMap()) {
        TriplTracking.send(eventType = "screen", name = type.value, fields = mapOf("type" to type.value, "id" to id?.value), properties = properties)
    }
}
```

TypeScript (`screenTracking.ts`):

```ts
import { triplSend } from './triplTransport';

export const ScreenType = {
  checkoutStart: 'Checkout Start',
  homeView: 'HomeView',
} as const;
export type ScreenType = (typeof ScreenType)[keyof typeof ScreenType];

export const ScreenId = {
  checkoutStart: 'checkout:start',
  home: 'home',
} as const;
export type ScreenId = (typeof ScreenId)[keyof typeof ScreenId];

export interface KnownEvent {
  readonly identity: string;
  readonly type: ScreenType;
}

/** Every planned screen view, checked by the compiler against the types above. */
export const knownEvents: readonly KnownEvent[] = [
  { identity: 'Checkout Start', type: ScreenType.checkoutStart },
  { identity: 'HomeView', type: ScreenType.homeView },
];

/**
 * The `screen` screen views (screen_view): the wrapper's own call, with its
 * arguments narrowed to the plan's screen types and ids.
 */
export function track(type: ScreenType, id?: ScreenId, properties: Readonly<Record<string, unknown>> = {}): void {
  triplSend('screen', type, { 'type': type, 'id': id }, properties);
}
```

### Named {#style-named}

One `track` call for every event. The event is a typed value that knows its
exact plan name and its properties. An event that fixes every field
(`checkout:start`) has no parameters: a Swift case without a payload, a Kotlin
`object`, a TypeScript `Record<string, never>`.

Swift (`LegacyTracking.swift`). `legacy` has a Swift transport,
`Analytics.shared.logEvent`, whose `calls` entry takes the name first and the
properties as `parameters:`:

```swift
/// The `legacy` events (named): one case per planned event, carrying
/// exactly the values the plan leaves open. Send one with `LegacyTracking.track(_:)`.
public enum LegacyEvent: Sendable {
    /// `Home Screen View`
    case homeScreenView(HomeScreenView)
    /// `checkout:start`
    @available(*, deprecated, message: "Deprecated in the tracking plan.")
    case checkoutStart
    /// `promo_sheet_${sheet_id}_shown`
    case promoSheetSheetIdShown(PromoSheetSheetIdShown)
    /// `trial_started`
    case trialStarted(TrialStarted)

    public enum Platform: String, CaseIterable, Sendable {
        case android = "android"
        case ios = "ios"
    }

    public enum SheetId: String, CaseIterable, Sendable {
        case spring = "spring"
        case summer = "summer"
    }

    public struct HomeScreenView: Sendable {
        public let mode: String
        public let isFirst: Bool?
        public let platform: Platform?

        public init(mode: String, isFirst: Bool? = nil, platform: Platform? = nil) {
            self.mode = mode
            self.isFirst = isFirst
            self.platform = platform
        }

        public var name: String { "Home Screen View" }

        public var properties: [String: Any] {
            var properties: [String: Any] = [:]
            properties["mode"] = mode
            if let isFirst { properties["is_first"] = isFirst }
            if let platform { properties["platform"] = platform.rawValue }
            return properties
        }
    }

    public struct PromoSheetSheetIdShown: Sendable {
        public let sheetId: SheetId
        public let isFirst: Bool?
        public let platform: Platform?

        public init(sheetId: SheetId, isFirst: Bool? = nil, platform: Platform? = nil) {
            self.sheetId = sheetId
            self.isFirst = isFirst
            self.platform = platform
        }

        public var name: String { "promo_sheet_\(sheetId.rawValue)_shown" }

        public var properties: [String: Any] {
            var properties: [String: Any] = [:]
            properties["mode"] = "promo_\(sheetId.rawValue)"
            if let isFirst { properties["is_first"] = isFirst }
            if let platform { properties["platform"] = platform.rawValue }
            return properties
        }
    }

    public struct TrialStarted: Sendable {
        public let isFirst: Bool?
        public let platform: Platform?

        public init(isFirst: Bool? = nil, platform: Platform? = nil) {
            self.isFirst = isFirst
            self.platform = platform
        }

        public var name: String { "trial_started" }

        public var properties: [String: Any] {
            var properties: [String: Any] = [:]
            properties["mode"] = "trial"
            if let isFirst { properties["is_first"] = isFirst }
            if let platform { properties["platform"] = platform.rawValue }
            return properties
        }
    }

    /// The event's name, exactly as the plan spells it.
    public var name: String { (self as any LegacyEventResolving).resolvedName }

    public var properties: [String: Any] { (self as any LegacyEventResolving).resolvedProperties }
}

/// Resolves every case — deprecated ones included — without a deprecation warning at
/// the switch: the switches live in deprecated members, reached through this protocol's
/// non-deprecated requirements, so warnings-as-errors builds still compile.
private protocol LegacyEventResolving {
    var resolvedName: String { get }
    var resolvedProperties: [String: Any] { get }
}

extension LegacyEvent: LegacyEventResolving {
    @available(*, deprecated, message: "Internal: use `name`.")
    fileprivate var resolvedName: String {
        switch self {
        case .homeScreenView(let event): return event.name
        case .checkoutStart: return "checkout:start"
        case .promoSheetSheetIdShown(let event): return event.name
        case .trialStarted(let event): return event.name
        }
    }

    @available(*, deprecated, message: "Internal: use `properties`.")
    fileprivate var resolvedProperties: [String: Any] {
        switch self {
        case .homeScreenView(let event): return event.properties
        case .checkoutStart: return ["is_first": true, "mode": "checkout", "platform": "ios"]
        case .promoSheetSheetIdShown(let event): return event.properties
        case .trialStarted(let event): return event.properties
        }
    }
}

public enum LegacyTracking {
    public static func track(_ event: LegacyEvent) {
        Analytics.shared.logEvent(event.name, parameters: event.properties)
    }
}
```

Kotlin (`LegacyTracking.kt`), through `TriplTracking.send`:

```kotlin
package com.example.tracking

/**
 * The `legacy` events (named): one type per planned event, carrying
 * exactly the values the plan leaves open. Send one with `LegacyTracking.track`.
 */
sealed interface LegacyEvent {
    /** The event's name, exactly as the plan spells it. */
    val name: String
    val properties: Map<String, Any?>

    enum class Platform(val value: String) {
        ANDROID("android"),
        IOS("ios");
    }

    enum class SheetId(val value: String) {
        SPRING("spring"),
        SUMMER("summer");
    }

    /** `Home Screen View` */
    data class HomeScreenView(
        val mode: String,
        val isFirst: Boolean? = null,
        val platform: Platform? = null,
    ) : LegacyEvent {
        override val name: String get() = "Home Screen View"
        override val properties: Map<String, Any?> get() = buildMap<String, Any?> {
            put("mode", mode)
            isFirst?.let { put("is_first", it) }
            platform?.let { put("platform", it.value) }
        }
    }

    /** `checkout:start` */
    @Deprecated("Deprecated in the tracking plan.")
    object CheckoutStart : LegacyEvent {
        override val name: String get() = "checkout:start"
        override val properties: Map<String, Any?> get() = mapOf("is_first" to true, "mode" to "checkout", "platform" to "ios")
    }

    /** `promo_sheet_${sheet_id}_shown` */
    data class PromoSheetSheetIdShown(
        val sheetId: SheetId,
        val isFirst: Boolean? = null,
        val platform: Platform? = null,
    ) : LegacyEvent {
        override val name: String get() = "promo_sheet_${sheetId.value}_shown"
        override val properties: Map<String, Any?> get() = buildMap<String, Any?> {
            put("mode", "promo_${sheetId.value}")
            isFirst?.let { put("is_first", it) }
            platform?.let { put("platform", it.value) }
        }
    }

    /** `trial_started` */
    data class TrialStarted(
        val isFirst: Boolean? = null,
        val platform: Platform? = null,
    ) : LegacyEvent {
        override val name: String get() = "trial_started"
        override val properties: Map<String, Any?> get() = buildMap<String, Any?> {
            put("mode", "trial")
            isFirst?.let { put("is_first", it) }
            platform?.let { put("platform", it.value) }
        }
    }
}

object LegacyTracking {
    fun track(event: LegacyEvent) {
        TriplTracking.send(eventType = "legacy", name = event.name, fields = emptyMap(), properties = event.properties)
    }
}
```

TypeScript (`legacyTracking.ts`). `LegacyEventProps` maps each plan name to
its properties, so the name picks the properties type. `FIXED` holds the
values the plan fixes, typed, and `triplInterpolate` fills the `${…}` holes of
the name and of fixed strings:

```ts
import { triplInterpolate, triplSend } from './triplTransport';

export const Platform = {
  android: 'android',
  ios: 'ios',
} as const;
export type Platform = (typeof Platform)[keyof typeof Platform];

export const SheetId = {
  spring: 'spring',
  summer: 'summer',
} as const;
export type SheetId = (typeof SheetId)[keyof typeof SheetId];

/**
 * The `legacy` events (named), keyed by their exact plan names: what each
 * takes is exactly the values the plan leaves open.
 */
export interface LegacyEventProps {
  /**
   * `Home Screen View`
   */
  readonly 'Home Screen View': {
    readonly 'mode': string;
    readonly 'is_first'?: boolean;
    readonly 'platform'?: Platform;
  };
  /**
   * `checkout:start`
   * @deprecated Deprecated in the tracking plan.
   */
  readonly 'checkout:start': Record<string, never>;
  /**
   * `promo_sheet_${sheet_id}_shown`
   */
  readonly 'promo_sheet_${sheet_id}_shown': {
    readonly 'sheet_id': SheetId;
    readonly 'is_first'?: boolean;
    readonly 'platform'?: Platform;
  };
  /**
   * `trial_started`
   */
  readonly 'trial_started': {
    readonly 'is_first'?: boolean;
    readonly 'platform'?: Platform;
  };
}

export type LegacyEventName = keyof LegacyEventProps;

const FIXED: { readonly [K in LegacyEventName]: Readonly<Record<string, unknown>> } = {
  'Home Screen View': {},
  'checkout:start': { 'is_first': true, 'mode': 'checkout', 'platform': 'ios' },
  'promo_sheet_${sheet_id}_shown': { 'mode': 'promo_${sheet_id}' },
  'trial_started': { 'mode': 'trial' },
};

const TOKENS: { readonly [K in LegacyEventName]: readonly string[] } = {
  'Home Screen View': [],
  'checkout:start': [],
  'promo_sheet_${sheet_id}_shown': ['sheet_id'],
  'trial_started': [],
};

export function track<K extends LegacyEventName>(name: K, props: LegacyEventProps[K]): void {
  const values = props as unknown as Readonly<Record<string, unknown>>;
  const resolvedName = triplInterpolate(name, values);
  const properties: Record<string, unknown> = {};
  for (const [key, fixed] of Object.entries(FIXED[name])) {
    properties[key] = typeof fixed === 'string' ? triplInterpolate(fixed, values) : fixed;
  }
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && !TOKENS[name].includes(key)) {
      properties[key] = value;
    }
  }
  triplSend('legacy', resolvedName, {}, properties);
}
```

### Self-describing {#style-self-describing}

One type per schema, carrying the schema URI and its data. The event's
`schema` is sent as the event name.

Swift (`SdTracking.swift`):

```swift
/// A `sd` event (self_describing): one type per schema, carrying exactly the
/// values the plan leaves open. Send one with `SdTracking.track(_:)`.
public protocol SdEvent: Sendable {
    /// The event's identity, exactly as the plan spells it.
    var name: String { get }
    /// The schema URI the event is sent with.
    var schema: String { get }
    var data: [String: Any] { get }
}

public enum SdTracking {
    /// `iglu:com.example/checkout_started/jsonschema/1-0-0`
    @available(*, deprecated, message: "Deprecated in the tracking plan.")
    public struct CheckoutStartedV1_0_0: SdEvent {
        public let sku: String
        public let price: Double?

        public init(sku: String, price: Double? = nil) {
            self.sku = sku
            self.price = price
        }

        public var name: String { "iglu:com.example/checkout_started/jsonschema/1-0-0" }
        public var schema: String { "iglu:com.example/checkout_started/jsonschema/1-0-0" }

        public var data: [String: Any] {
            var properties: [String: Any] = [:]
            properties["sku"] = sku
            if let price { properties["price"] = price }
            return properties
        }
    }

    /// `iglu:com.example/checkout_started/jsonschema/2-0-0`
    public struct CheckoutStartedV2_0_0: SdEvent {
        public let sku: String
        public let price: Double?

        public init(sku: String, price: Double? = nil) {
            self.sku = sku
            self.price = price
        }

        public var name: String { "iglu:com.example/checkout_started/jsonschema/2-0-0" }
        public var schema: String { "iglu:com.example/checkout_started/jsonschema/2-0-0" }

        public var data: [String: Any] {
            var properties: [String: Any] = [:]
            properties["sku"] = sku
            if let price { properties["price"] = price }
            return properties
        }
    }

    /// `iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0`
    public struct PromoSheetViewed: SdEvent {

        public init() {
        }

        public var name: String { "iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0" }
        public var schema: String { "iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0" }

        public var data: [String: Any] {
            var properties: [String: Any] = [:]
            properties["price"] = 4.5
            properties["sku"] = "none"
            return properties
        }
    }

    public static func track(_ event: some SdEvent) {
        TriplTracking.send(eventType: "sd", name: event.schema, fields: [:], properties: event.data)
    }
}
```

Kotlin (`SdTracking.kt`):

```kotlin
package com.example.tracking

/**
 * A `sd` event (self_describing): one class per schema, carrying exactly the
 * values the plan leaves open. Send one with `SdTracking.track`.
 */
sealed interface SdEvent {
    /** The event's identity, exactly as the plan spells it. */
    val name: String
    /** The schema URI the event is sent with. */
    val schema: String
    val data: Map<String, Any?>

    /** `iglu:com.example/checkout_started/jsonschema/1-0-0` */
    @Deprecated("Deprecated in the tracking plan.")
    data class CheckoutStartedV1_0_0(
        val sku: String,
        val price: Double? = null,
    ) : SdEvent {
        override val name: String get() = "iglu:com.example/checkout_started/jsonschema/1-0-0"
        override val schema: String get() = "iglu:com.example/checkout_started/jsonschema/1-0-0"
        override val data: Map<String, Any?> get() = buildMap<String, Any?> {
            put("sku", sku)
            price?.let { put("price", it) }
        }
    }

    /** `iglu:com.example/checkout_started/jsonschema/2-0-0` */
    data class CheckoutStartedV2_0_0(
        val sku: String,
        val price: Double? = null,
    ) : SdEvent {
        override val name: String get() = "iglu:com.example/checkout_started/jsonschema/2-0-0"
        override val schema: String get() = "iglu:com.example/checkout_started/jsonschema/2-0-0"
        override val data: Map<String, Any?> get() = buildMap<String, Any?> {
            put("sku", sku)
            price?.let { put("price", it) }
        }
    }

    /** `iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0` */
    object PromoSheetViewed : SdEvent {
        override val name: String get() = "iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0"
        override val schema: String get() = "iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0"
        override val data: Map<String, Any?> get() = mapOf("price" to 4.5, "sku" to "none")
    }
}

object SdTracking {
    fun track(event: SdEvent) {
        TriplTracking.send(eventType = "sd", name = event.schema, fields = emptyMap(), properties = event.data)
    }
}
```

TypeScript (`sdTracking.ts`):

```ts
import { triplInterpolate, triplSend } from './triplTransport';

/**
 * The `sd` events (self_describing), keyed by their exact plan names: each
 * schema's data is exactly the values the plan leaves open.
 */
export interface SdEventProps {
  /**
   * `iglu:com.example/checkout_started/jsonschema/1-0-0`
   * @deprecated Deprecated in the tracking plan.
   */
  readonly 'iglu:com.example/checkout_started/jsonschema/1-0-0': {
    readonly 'sku': string;
    readonly 'price'?: number;
  };
  /**
   * `iglu:com.example/checkout_started/jsonschema/2-0-0`
   */
  readonly 'iglu:com.example/checkout_started/jsonschema/2-0-0': {
    readonly 'sku': string;
    readonly 'price'?: number;
  };
  /**
   * `iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0`
   */
  readonly 'iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0': Record<string, never>;
}

export type SdEventName = keyof SdEventProps;
export type CheckoutStartedV1_0_0 = SdEventProps['iglu:com.example/checkout_started/jsonschema/1-0-0'];
export type CheckoutStartedV2_0_0 = SdEventProps['iglu:com.example/checkout_started/jsonschema/2-0-0'];
export type PromoSheetViewed = SdEventProps['iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0'];

const SCHEMAS: { readonly [K in SdEventName]: string } = {
  'iglu:com.example/checkout_started/jsonschema/1-0-0': 'iglu:com.example/checkout_started/jsonschema/1-0-0',
  'iglu:com.example/checkout_started/jsonschema/2-0-0': 'iglu:com.example/checkout_started/jsonschema/2-0-0',
  'iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0': 'iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0',
};

const FIXED: { readonly [K in SdEventName]: Readonly<Record<string, unknown>> } = {
  'iglu:com.example/checkout_started/jsonschema/1-0-0': {},
  'iglu:com.example/checkout_started/jsonschema/2-0-0': {},
  'iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0': { 'price': 4.5, 'sku': 'none' },
};

const TOKENS: { readonly [K in SdEventName]: readonly string[] } = {
  'iglu:com.example/checkout_started/jsonschema/1-0-0': [],
  'iglu:com.example/checkout_started/jsonschema/2-0-0': [],
  'iglu:com.example/promo_sheet_viewed/jsonschema/1-0-0': [],
};

export function track<K extends SdEventName>(name: K, props: SdEventProps[K]): void {
  const values = props as unknown as Readonly<Record<string, unknown>>;
  const schema = triplInterpolate(SCHEMAS[name], values);
  const data: Record<string, unknown> = {};
  for (const [key, fixed] of Object.entries(FIXED[name])) {
    data[key] = typeof fixed === 'string' ? triplInterpolate(fixed, values) : fixed;
  }
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && !TOKENS[name].includes(key)) {
      data[key] = value;
    }
  }
  triplSend('sd', schema, {}, data);
}
```

### Deprecated events {#deprecated-events}

A deprecated event is generated with the language's own deprecation marker, so
every call that creates one compiles with a warning:

- **Kotlin**: `@Deprecated("Deprecated in the tracking plan.")` on its class or
  object.
- **TypeScript**: `@deprecated Deprecated in the tracking plan.` in the JSDoc
  of its key in the props interface.
- **Swift, `self_describing`**: `@available(*, deprecated, message:
  "Deprecated in the tracking plan.")` on its struct.
- **Swift, `named`**: the same attribute on its enum case. The generated code
  itself has to switch over that case to compute `name` and `properties`, so
  when an enum has a deprecated case those two switches move out of the public
  members. `name` and `properties` read through a private protocol
  (`LegacyEventResolving` above), and the switches live in `fileprivate`
  members, `resolvedName` and `resolvedProperties`, that are themselves marked
  deprecated and satisfy that protocol's requirements, which are not. The
  intent is that the generated file adds no deprecation warning of its own,
  so a build with warnings as errors still compiles, while your own use of a
  deprecated case still warns. An enum with no deprecated case keeps a plain
  `switch` in `name` and `properties`.

## The transport {#transport}

The generated code sends nothing itself. It hands every event either to your
own wrapper (a `transport`) or to the shared [`TriplDestination`](#destination).

A `transport` entry is per language. Its value is either a bare function or an
object:

| Form | Meaning |
|------|---------|
| `"Analytics.shared.log"` | The function to call. Its argument mapping is taken from the event type's `calls` entry with the same `function` (and, if that entry has `languages`, one that includes this language). With no such entry, the style's default arguments are used: for `structured` and `screen_view`, the plan's fields in order, then `properties`. |
| `{function, import, args, positional, object_arg, name_arg, properties_arg}` | `function` is required. `args`, `positional`, `object_arg`, `name_arg` and `properties_arg` mean what they mean in a [`calls` entry](./tripl-check.md#config-file). Without `args` or `positional`, the mapping falls back to the matching `calls` entry, as for a bare function. `import` is one line copied verbatim to the top of the generated Kotlin or TypeScript file (Swift ignores it, because the generated file is in the same module as your wrapper). |

`function` is a dotted call path (`Analytics.shared.log`, `analytics.log`),
used as written. Arguments are spelled the way the wrapper is called:

- **Swift**: positional arguments unlabelled, the rest `label: value`.
- **Kotlin**: positional arguments, then named arguments `label = value`. Give
  `positional` for a Java wrapper.
- **TypeScript**: positional only. With `object_arg`, the labelled arguments
  become one object literal at that position, dotted labels nesting
  (`event.schema` gives `{ event: { schema: … } }`).

A target is a plan field, `properties`, `name` (the event's name; for
`structured` and `screen_view`, built from the name rule), `name:iglu` or
`value:number`. `value:number` is Snowplow's numeric `value`: for
`structured` and `screen_view`, the generated function gets an optional
`value: Double? = nil` (Swift), `value: Double? = null` (Kotlin) or
`value?: number` (TypeScript) parameter that is forwarded as it is. It is not a
plan field, so `tripl check` ignores it, and `field:value` still means a plan
field called `value`. See [The `value` role](#value-role). The fixture's `se` entry shows all three forms:
a bare Swift function reusing the `calls` entry, a Kotlin object with `args`,
and a TypeScript object with `positional` and `import`.

### The shared seam: `TriplDestination` {#destination}

Every run writes one shared file per language, next to the generated files:
`TriplTransport.swift`, `TriplTransport.kt` and `triplTransport.ts`. It
declares `TriplDestination`, the interface every generated tracker without a
`transport` forwards to. You implement it once, over your own analytics
wrapper, and install it at startup:

- Swift: assign your implementation to `TriplTracking.destination`.
- Kotlin: assign it to `TriplTracking.destination` (it is a `fun interface`,
  so a lambda works).
- TypeScript: pass it to `setTriplDestination`.

Until a destination is set, calls through the generated trackers are dropped.
`track` receives the plan's event type name, so one implementation can route
different types to different SDKs; `name` is the event's identity (for a
self-describing event, its schema URI), or `nil` / `null` / `undefined` when
the event type has no name rule to build one from; `fields` are the plan's
field values as strings, with unset ones left out; `properties` is everything
else the call carried.

Swift (`TriplTransport.swift`):

```swift
/// Where generated trackers send an event whose type has no `transport` configured in
/// `.tripl/check.yml`. Implement it once, over the app's own analytics wrapper, and set
/// `TriplTracking.destination` at startup.
public protocol TriplDestination: AnyObject {
    /// - Parameters:
    ///   - eventType: the plan's event type name.
    ///   - name: the event's identity (a self-describing event's schema URI); `nil` when the
    ///     event type has no name rule to build one from.
    ///   - fields: the plan's field values, as strings.
    ///   - properties: everything else the call carried.
    func track(eventType: String, name: String?, fields: [String: String], properties: [String: Any])
}

public enum TriplTracking {
    /// Until it is set, calls through the generated trackers are dropped.
    nonisolated(unsafe) public static var destination: (any TriplDestination)?

    public static func send(
        eventType: String,
        name: String?,
        fields: [String: String?],
        properties: [String: Any]
    ) {
        destination?.track(
            eventType: eventType,
            name: name,
            fields: fields.compactMapValues { $0 },
            properties: properties
        )
    }
}
```

Kotlin (`TriplTransport.kt`):

```kotlin
package com.example.tracking

/**
 * Where generated trackers send an event whose type has no `transport` configured in
 * `.tripl/check.yml`. Implement it once, over the app's own analytics wrapper, and set
 * [TriplTracking.destination] at startup.
 *
 * `name` is the event's identity (a self-describing event's schema URI), or null when the
 * event type has no name rule to build one from; `fields` are the plan's field values.
 */
fun interface TriplDestination {
    fun track(eventType: String, name: String?, fields: Map<String, String>, properties: Map<String, Any?>)
}

object TriplTracking {
    /** Until it is set, calls through the generated trackers are dropped. */
    @Volatile
    var destination: TriplDestination? = null

    fun send(eventType: String, name: String?, fields: Map<String, String?>, properties: Map<String, Any?>) {
        val present = buildMap<String, String> {
            fields.forEach { (key, value) -> if (value != null) put(key, value) }
        }
        destination?.track(eventType, name, present, properties)
    }
}
```

TypeScript (`triplTransport.ts`). It also exports `triplInterpolate`, which
the `named` and `self_describing` files use to fill `${…}` holes:

```ts
/**
 * Where generated trackers send an event whose type has no `transport` configured in
 * `.tripl/check.yml`. Implement it once, over the app's own analytics wrapper, and pass it
 * to `setTriplDestination` at startup.
 *
 * `name` is the event's identity (a self-describing event's schema URI), or undefined when
 * the event type has no name rule to build one from; `fields` are the plan's field values.
 */
export interface TriplDestination {
  track(
    eventType: string,
    name: string | undefined,
    fields: Readonly<Record<string, string>>,
    properties: Readonly<Record<string, unknown>>,
  ): void;
}

let destination: TriplDestination | undefined;

/** Until it is set, calls through the generated trackers are dropped. */
export function setTriplDestination(next: TriplDestination | undefined): void {
  destination = next;
}

export function triplSend(
  eventType: string,
  name: string | undefined,
  fields: Readonly<Record<string, string | undefined>>,
  properties: Readonly<Record<string, unknown>>,
): void {
  if (destination === undefined) {
    return;
  }
  const present: Record<string, string> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined) {
      present[key] = value;
    }
  }
  destination.track(eventType, name, present, properties);
}

/**
 * Fills the `${token}` holes of a plan string from the values a call passed. The token
 * grammar is the plan's own: `${` then anything but `}`, then `}`.
 */
export function triplInterpolate(
  template: string,
  values: Readonly<Record<string, unknown>>,
): string {
  return template.replace(/\$\{([^}]*)\}/g, (whole: string, key: string) => {
    const value = values[key];
    return value === undefined ? whole : String(value);
  });
}
```

## Where the files go {#output}

Each event type writes one file per language, named after its `namespace`
type: `<Namespace>.swift`, `<Namespace>.kt`, and `<namespace>.ts` in
lowerCamelCase. The default namespace is the event type's name followed by
`Tracking` (`screen` gives `ScreenTracking`), and the default `event` type is
the name followed by `Event` (`LegacyEvent`). Each language directory also gets
the [shared transport file](#destination) for that language. Two event types
that would write the same file are a configuration error; give one of them
`type_names: {namespace: …}`.

The directory comes from `--out DIR` or the top-level `codegen.out`:

- `--out DIR` is DIR itself when the run writes one language, and `DIR/<lang>`
  when it writes several.
- `codegen.out` as one directory is always `DIR/<lang>`. As a map, each
  language goes to its own directory.

A run also removes **stale** files: files it generated earlier and no longer
produces, for example after an event type left the configuration. A file is
only ever removed when its header says it was generated by tripl codegen **for
the same project**, and it is in a language this run writes into that
directory. A hand-written file next to the generated ones, another project's
generated files and a language the run skipped (`--lang swift` never touches a
`.kt` file) are left alone.

## Identifier naming {#identifier-naming}

Plan values are free text: `1st run`, `Home Screen View`, `checkout:start`,
`promo_sheet_${sheet_id}_shown`. Identifiers are derived from them by the same
rules in every language:

1. Split the value on every run of characters that are not ASCII letters or
   digits, then split each piece again at camelCase boundaries.
   `Home Screen View` gives `Home`, `Screen`, `View`; `checkout:start` gives
   `checkout`, `start`; `HomeView` gives `Home`, `View`.
2. Join the words in the language's case:

   | Used for | Case | `Home Screen View` | `checkout:start` |
   |----------|------|--------------------|------------------|
   | Swift and TypeScript enum cases, parameters, Swift `named` cases | lowerCamel | `homeScreenView` | `checkoutStart` |
   | Types (Swift, Kotlin, TypeScript), Kotlin data classes | UpperCamel | `HomeScreenView` | `CheckoutStart` |
   | Kotlin enum entries | UPPER_SNAKE | `HOME_SCREEN_VIEW` | `CHECKOUT_START` |

3. A value that starts with a digit gets a leading `_` (`1st run` gives
   `_1stRun`, `_1ST_RUN`). A value with no ASCII letters or digits at all gets
   `value`.
4. **Reserved words** get a trailing `_` (`default` gives `default_`), and a
   type name the language already uses (`String`, `Type`) gets `Value`
   (`TypeValue`). A suffix rather than Swift or Kotlin backticks, because a
   suffixed name is valid everywhere it is used, and a backticked one is not.
5. **Collisions** get a numeric suffix. `promo_sheet` and `promo-sheet` both
   give `promoSheet`: the one that sorts first keeps it, and the other becomes
   `promoSheet2`, then `promoSheet3`. Values are sorted before naming, so the
   numbering is the same on every run.

A `${token}` follows the same grammar as the plan: `${`, then anything but
`}`, then `}`. In a name rule, a key is `{…}` with at least one character, and
the braces of a `${…}` token are never read as one. The token's name becomes a
parameter by the same rules (`${sheet_id}` gives `sheetId`).

TypeScript property keys and literal types are the quoted plan names
themselves, so they never need any of this. The raw value, the string that is
actually sent, is always the exact plan string.

To rename a generated **type** or the function, use `type_names`. There is no
per-case override: if a derived case name reads badly, the cleaner fix is
usually the plan value.

## Custom templates {#custom-templates}

Each style and language has a built-in template, shipped inside the CLI
package. To change what is generated for one event type (a different base
class, extra conformances, a logging line), point that event type's
`template` at your own file: `template: {swift: PATH}` replaces the Swift
template only, and the other languages keep the built-in ones. A single path
(`template: PATH`) is allowed when the event type's `languages` has exactly
one entry.

A custom template is opt-in and per event type: without one, the built-in,
industry-standard output is generated and stays as it is. When your code base
needs a different shape altogether (constant holders, your own event object,
screens as (type, id) pairs, name constants), see
[Building your own shape](#own-shape).

A good start is to copy the built-in template for the same style and language
from the installed package (`tripl_cli/codegen/templates/`, named like
`structured.swift.mustache`) and edit it. To iterate without an instance, save
the plan once with `tripl export --format codegen_model --out DIR` and run
`tripl codegen --model DIR/codegen_model.json`.

Templates use a **small subset of Mustache**, implemented in the CLI itself (it
adds no dependency):

| Tag | Meaning |
|-----|---------|
| `{{name}}` | Insert a value as it is. Nothing is HTML-escaped: this renders source code. Dotted names (`{{event_type.name}}`) read into objects, and `{{.}}` is the current item. `{{&name}}` means the same. |
| `{{#name}}…{{/name}}` | For a list, render the block once per item, with the item's keys in scope. For an object, render it once with its keys in scope. For any other true value, render it once. A missing name, `null`, `false`, `""` and an empty list render nothing. |
| `{{^name}}…{{/name}}` | Render the block only when the name is missing or empty, as above. |
| `{{! comment }}` | Dropped. |

A section, closing or comment tag alone on its line takes the whole line with
it, so templates can be laid out legibly without blank lines leaking into the
output. Partials, lambdas and delimiter changes are not supported. A name the
context does not have is an **error** that names the template file and line,
not an empty string: a typo must not generate code with a hole in it.

**Escaping is done before the template sees a value.** A plan string that ends
up in code arrives already quoted or escaped for the target language. The
built-in templates for the same style and language are the complete reference
for every key they use; [The template context](#template-context) lists every
key, including those only templates of your own need.

### Building your own shape {#own-shape}

**The defaults do not change.** Without a `template`, every style generates
the industry-standard shape described above: one enum per closed field, a
typed function per event type, one case or class per event. There is no
switch for another shape (no "constants instead of enums" flag, no "screens as
pairs" mode), and none is planned: a code base that already has its own shape
builds it itself, with templates of its own. Everything below is opt-in and
only takes effect for an event type whose `template` or `files` points at your
own files.

What a team's own shape needs, the context gives it: the plan's fields,
values, events and planned combinations with every string already quoted and
already named in four casings (`plan`), the configured wrapper call with its
arguments (`transport`), strings of your own (`vars`), more than one file per
event type (`files`), and Snowplow's numeric `value` as an argument
(`value:number`).

The CLI's test fixture `cli/tests/codegen/custom/` builds four such shapes
from templates only, and its `check.yml`, `templates/` and `golden/` are a
working starting point. The [recipes](#own-shape-recipes) below are copied from
it.

#### `vars` {#template-vars}

`vars` is a flat map of names to **strings**, set in the top-level `codegen`
block and, per event type, in its `codegen` block. A template reads it as
`{{vars.NAME}}`. An event type's own `vars` are merged over the top-level
ones, key by key:

```yaml
codegen:
  vars: {prefix: Acme, prefix_lower: acme, event_class: AcmeEvent}
event_types:
  app:
    codegen:
      style: named
      vars: {prefix: AcmeApp}        # this event type only: AcmeApp…, the rest as above
```

A name is letters, digits and `_`, not starting with a digit. A value must be
a string: quote a number or boolean (`version: "1.0"`), or the configuration is
rejected (exit 2). The built-in templates never read `vars`.

#### `files` {#template-files}

An event type writes one file of its own (named after `namespace`). To write
more, list them under its `files`; each is rendered from your template with the
same context as the event type's own file and written next to it:

```yaml
codegen:
  files:
    - {language: kotlin, template: templates/holder.kotlin.mustache,
       each: closed_fields, file: "{{vars.prefix}}{{names.pascal}}.kt"}
    - {language: swift, template: templates/planned.swift.mustache,
       file: AcmePlanned.swift}
```

| Key | What it is |
|-----|------------|
| `language` | Required. `swift`, `kotlin` or `ts`, and one of the event type's `languages`. `--lang` skips the entries of the other languages. |
| `template` | Required. The template file, relative to the directory that holds `.tripl/` like `template`. |
| `file` | Required. A bare file name with the language's extension (`.swift`, `.kt`, `.ts`): no directory, no leading `.`, only letters, digits, `_`, `-` and `.`. It may use tags, and the rendered name is checked again. Names must be unique, ignoring case, among the event type's files. |
| `each` | Optional. `fields`, `closed_fields` (the fields whose every value is known) or `events`: render the entry once per item of `plan.fields` or `plan.events`, with the item's keys in scope (and the item itself as `item`). The file name must then contain a tag, so every item gets its own file. |

The rendered file must start with `{{header}}`, or the run fails (exit 2).
That header is how `--check` compares the file and how stale-file cleanup
knows it: a file you still configure is never removed, and one you drop from
`files` is removed like any other stale file.

#### The `value` role {#value-role}

Snowplow's structured event has a numeric `value` that is not a plan field. The
argument target `value:number` gives it a role in the argument mapping of the
language's `transport`: either the transport's own `args`/`positional`, or the
`calls` entry that a bare-function `transport` reuses:

```yaml
event_types:
  se:
    calls:
      - function: "AcmeEvent"
        args: {category: category, action: action, label: label, property: property, value: "value:number"}
    codegen:
      transport:
        kotlin: "AcmeEvent"      # a bare function: reuses the `calls` entry above
```

For `structured` and `screen_view`, the generated function then takes an
optional `value: Double? = nil` (Swift), `value: Double? = null` (Kotlin) or
`value?: number` (TypeScript) parameter and forwards it as it is. A language
with **no** `transport` forwards to `TriplDestination`, which carries plan
fields only: there `value:number` in `calls` has no effect, and the generated
function has no numeric parameter. In the
context, that parameter and that argument have the role `value` (`is_value`).
`tripl check` ignores the argument, since it is not a plan field, and
`field:value` still means a plan field called `value`. For `named` and
`self_describing`, `value:number` is a configuration error.

#### The template context {#template-context}

Every key a template can read, by where it comes from. A key the context does
not have is an error, not an empty string. A key that has no value for one item
(a positional argument's `label`, say) is **absent** from that item, so
`{{label}}` there is an error too; test it with `{{#label}}…{{/label}}` or
`{{^label}}…{{/label}}`.

**Every style:**

| Key | What it holds |
|-----|---------------|
| `header` | The generated-file header. A file from `files` must start with it. |
| `kotlin_package` | The top-level `kotlin_package`, for Kotlin; empty otherwise. |
| `imports` | Lines to import (`line`): the transport's `import` (not for Swift) and, for TypeScript, what the file needs from `triplTransport.ts`. `has_imports` says whether there are any. |
| `event_type` | `raw` (the name, safe in a comment), `literal` (quoted for the language), `display_name`, `names`. |
| `style` | `structured`, `screen_view`, `named` or `self_describing`. |
| `uses_destination` | True when the event type has no `transport` for this language. |
| `namespace` | The enclosing type and file name (`type_names.namespace`, or derived from the event type). |
| `transport` | The configured wrapper for this language; absent without one: see below. |
| `plan` | The plan, independent of the style: see below. |
| `vars` | The merged `vars`. |
| `language` | `swift`, `kotlin` or `ts`. |

**`transport`** (absent when the event type has no `transport` for this
language):

| Key | What it holds |
|-----|---------------|
| `function` | The configured function, as written. |
| `import` | The configured `import` line; absent without one. |
| `object_arg` | The TypeScript `object_arg` position; absent without one. |
| `args`, `has_args` | The arguments in call order. Each has `label` (absent when positional), `positional`, `role` (`field`, `name`, `name_iglu`, `properties`, `value` or `none`), the flags `is_field`, `is_name` (for `name` and `name_iglu`), `is_properties`, `is_value`, `is_none`, and for a field `target` and `names` (the field's names, as in `plan.fields`); `param` (for `structured` and `screen_view`: the generated parameter it forwards; absent when none does, as for `name`) and `expression` (what the built-in template passes). |

**`plan`**:

| Key | What it holds |
|-----|---------------|
| `plan.event_type` | `raw`, `literal`, `names`. |
| `plan.fields`, `plan.has_fields` | The event type's fields, which are also its property keys: `raw`, `literal`, `names`, `type`, `required`, `closed` (every value is known), `variable` (absent when the field has none), `values` (each `raw`, `literal`, `names`) and `has_values`. |
| `plan.field.<key>` | The same field item, by the field's **key**: its name in lower_snake words, with a leading digit prefixed by `_` and nothing else (`plan.field.category`, `plan.field.promo_code` for `promo-code`, `plan.field.default` in every language). Unlike `names.lower_snake`, a key is never escaped or numbered, so it is the same in every language; when two fields have one key, the first in plan order keeps it. |
| `plan.events`, `plan.has_events` | Each event: `raw`, `literal`, `names` (of its identity), `deprecated`, `dynamic` (the identity has a `${token}`), `name_expr` (the identity as an expression over the token parameters), `values` (each value the event sets: `field` with `raw`, `literal`, `names`; `raw`, `literal`, `names`, `dynamic`), `value.<key>` (the same, by the field's key, as for `plan.field`), `tokens` and `has_tokens` (each `raw`, `literal`, `names`, `closed`, `values`), `properties` and `has_properties` (every field, with `raw`, `literal`, `names`, `required`, `fixed` and the fixed `value`, absent when the event does not fix it). |
| `plan.combinations`, `plan.has_combinations` | Every planned combination: one per event, and an event whose `${token}` has allowed values once per value (an event with a free token, or with more than 1000 combinations, is left out). Each has `raw`, `literal`, `names` (of the expanded identity), `event` (`raw`, `literal`, `names` of the event), `deprecated`, `values` and `value.<key>` as for events. |

**`structured` and `screen_view`** add:

| Key | What it holds |
|-----|---------------|
| `enums` | The generated enums: `name`, and `cases` with `ident`, `raw`, `literal`, `sep`, `last`, `names`. |
| `function` | `name`, `signature`, `forward` (the call the built-in template makes) and `params`, each with `ident`, `type`, `optional`, `default`, `role` (`field`, `properties` or `value`), `is_field`, `is_properties`, `is_value`, `is_enum` (the parameter is a generated enum), for a field `target` (the field), `text` (the parameter as the plan string) and `names` (the field's names, as in `plan.fields`; all three absent for `properties` and `value`), `last`, `comma`. |
| `known` | Nothing, or the typed known-event list: `fields` (`ident`, `type`, `raw`, `literal`, `names` as in `plan.fields`) and `events` (`args`, `identity`, `identity_literal`, `deprecated`, `names`). |

**`named` and `self_describing`** add `event_name`, `props_type`,
`names_type`, `enums` (as above), `has_deprecated`, `resolver`, `function`
(`name`, `forward`, `uses_name`, `uses_schema`) and `events`, each with
`case`, `class_name`, `identity`, `identity_literal`, `schema_literal`,
`deprecated`, `names`, `props` and `has_props` (each prop `ident`, `type`,
`base_type`, `optional`, `key`, `sep`, `init`, `names`), `init_signature`,
`name_expr`, `schema_expr`, `puts` (`stmt`) and `has_puts`,
`properties_literal`, `fixed` (`key`, `value`), `tokens` (`literal`) and
`tokens_literal`.

**`names`** holds four spellings of a plan string: `upper_snake`,
`lower_snake`, `camel` and `pascal`. Each is already a valid identifier in the
target language: a leading digit gets `_` (`_1stCard`), a reserved word gets
`_` (`default_`, `class_`), in Swift `camel` also avoids the members Swift
synthesizes (`rawValue_`, `allCases_`, `hashValue_`), and `pascal` follows the
type-name rule of [Identifier naming](#identifier-naming). Each is unique
within its list: two values that land on the same name get `2`, `3`, … in every
spelling alike (`promo-sheet` and `promo_sheet` give `promoSheet` and
`promoSheet2`). A field has the same names wherever it appears (`plan.fields`,
`function.params`, `transport.args`, `known.fields`), so a member declared from
one list is the member another list refers to. Names you add yourself are not
checked: an enum or class of your own may still have members of its own a
case must not take. A value
inside an event or a combination has the names its field's `values` gave it,
so `AcmeLabel.{{value.label.names.camel}}` in one file refers to the constant
another file declared.

For separators, every item of `transport.args` and `function.params` has
`last` and `comma` (`,` except on the last item), and every item of a `plan`
list has `first` as well.

#### Recipes {#own-shape-recipes}

Each recipe shows the configuration, one template and what it generates, all
taken from `cli/tests/codegen/custom/` (project `acme`, with
`kotlin_package: com.example.acme` and
`vars: {prefix: Acme, prefix_lower: acme, event_class: AcmeEvent}`). The
fixture has the same shapes for Swift and TypeScript as well.

##### Constant holders instead of enums {#recipe-constant-holders}

One `object` of string constants per closed field, one file each, instead of
one enum per field. A `files` entry with `each: closed_fields` renders the
template once per field:

```yaml
event_types:
  se:
    codegen:
      style: structured
      files:
        - {language: kotlin, template: templates/holder.kotlin.mustache, each: closed_fields, file: "{{vars.prefix}}{{names.pascal}}.kt"}
        - {language: ts, template: templates/holder.ts.mustache, each: closed_fields, file: "{{vars.prefix_lower}}{{names.pascal}}.ts"}
```

`templates/holder.kotlin.mustache`:

```text
{{header}}
{{#kotlin_package}}

package {{kotlin_package}}
{{/kotlin_package}}

/** The plan's values for `{{raw}}` ({{event_type.raw}}). */
object {{vars.prefix}}{{names.pascal}} {
{{#values}}
    const val {{names.pascal}} = {{literal}}
{{/values}}
}
```

`AcmeCategory.kt`, one of the four files it writes (with `AcmeAction.kt`,
`AcmeLabel.kt` and `AcmeProperty.kt`):

```kotlin
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: se (structured). Regenerate with `tripl codegen`.

package com.example.acme

/** The plan's values for `category` (se). */
object AcmeCategory {
    const val Checkout = "checkout"
    const val Home = "home"
    const val Paywall = "paywall"
}
```

The values' `names` are already escaped and unique: in `AcmeLabel.kt`,

```kotlin
object AcmeLabel {
    const val _1stCard = "1st_card"
    const val Default = "default"
    const val Offer = "offer"
    const val PayButton = "pay_button"
    const val Paywall = "paywall"
    const val PromoSheet = "promo-sheet"
    const val PromoSheet2 = "promo_sheet"
}
```

`templates/holder.ts.mustache` writes the same holder as a TypeScript `const`
object with a matching union type:

```text
{{header}}

/** The plan's values for `{{raw}}` ({{event_type.raw}}). */
export const {{vars.prefix}}{{names.pascal}} = {
{{#values}}
  {{names.camel}}: {{literal}},
{{/values}}
} as const;
export type {{vars.prefix}}{{names.pascal}} = (typeof {{vars.prefix}}{{names.pascal}})[keyof typeof {{vars.prefix}}{{names.pascal}}];
```

```ts
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: se (structured). Regenerate with `tripl codegen`.

/** The plan's values for `category` (se). */
export const AcmeCategory = {
  checkout: 'checkout',
  home: 'home',
  paywall: 'paywall',
} as const;
export type AcmeCategory = (typeof AcmeCategory)[keyof typeof AcmeCategory];
```

##### A factory returning your own event object {#recipe-factory}

The generated function builds the team's own event value and returns it
instead of sending it, with the numeric `value` as a
[`value:number`](#value-role) argument. The `transport` names the
constructor, and the template spells the call from `transport.args` and the
return type from `vars.event_class`:

```yaml
event_types:
  se:
    calls:
      - function: "AcmeEvent"
        args: {category: category, action: action, label: label, property: property, value: "value:number"}
    codegen:
      style: structured
      transport:
        kotlin: "AcmeEvent"
        swift: "AcmeEvent"
        ts:
          function: "acmeEvent"
          object_arg: 0
          args: {category: category, action: action, label: label, property: property, value: "value:number"}
          import: "import { acmeEvent, type AcmeEvent } from './acmeEvent';"
      type_names: {namespace: AcmeStructured, function: event}
      template:
        kotlin: templates/structured.kotlin.mustache
        swift: templates/structured.swift.mustache
        ts: templates/structured.ts.mustache
```

`templates/structured.kotlin.mustache`:

```text
{{header}}
{{#kotlin_package}}

package {{kotlin_package}}
{{/kotlin_package}}

/**
 * Builds a `{{event_type.raw}}` event as the team's own {{vars.event_class}}, to be handed
 * on; pass the constants of {{#plan.fields}}{{#closed}}{{vars.prefix}}{{names.pascal}}{{^last}}, {{/last}}{{/closed}}{{/plan.fields}}.
 */
object {{namespace}} {
    fun {{function.name}}(
{{#function.params}}
        {{ident}}: {{#is_field}}String{{#optional}}? = null{{/optional}}{{/is_field}}{{#is_value}}Double? = null{{/is_value}}{{comma}}
{{/function.params}}
    ): {{vars.event_class}} = {{transport.function}}(
{{#transport.args}}
        {{label}} = {{param}}{{comma}}
{{/transport.args}}
    )
}
```

`AcmeStructured.kt`:

```kotlin
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: se (structured). Regenerate with `tripl codegen`.

package com.example.acme

/**
 * Builds a `se` event as the team's own AcmeEvent, to be handed
 * on; pass the constants of AcmeCategory, AcmeAction, AcmeLabel, AcmeProperty.
 */
object AcmeStructured {
    fun event(
        category: String,
        action: String,
        label: String? = null,
        property: String? = null,
        value: Double? = null
    ): AcmeEvent = AcmeEvent(
        category = category,
        action = action,
        label = label,
        property = property,
        value = value
    )
}
```

The TypeScript template types each closed field with its holder type
(`is_enum`), importing it from the holder's file:

```text
{{header}}

{{#imports}}
{{line}}
{{/imports}}
{{#plan.fields}}
{{#closed}}
import type { {{vars.prefix}}{{names.pascal}} } from './{{vars.prefix_lower}}{{names.pascal}}';
{{/closed}}
{{/plan.fields}}

/** Builds a `{{event_type.raw}}` event as the team's own {{vars.event_class}}, to be handed on. */
export function {{function.name}}(
{{#function.params}}
  {{ident}}{{#optional}}?{{/optional}}: {{#is_field}}{{#is_enum}}{{vars.prefix}}{{names.pascal}}{{/is_enum}}{{^is_enum}}string{{/is_enum}}{{/is_field}}{{#is_value}}number{{/is_value}},
{{/function.params}}
): {{vars.event_class}} {
  return {{transport.function}}({ {{#transport.args}}{{label}}: {{param}}{{^last}}, {{/last}}{{/transport.args}} });
}
```

```ts
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: se (structured). Regenerate with `tripl codegen`.

import { acmeEvent, type AcmeEvent } from './acmeEvent';
import type { AcmeCategory } from './acmeCategory';
import type { AcmeAction } from './acmeAction';
import type { AcmeLabel } from './acmeLabel';
import type { AcmeProperty } from './acmeProperty';

/** Builds a `se` event as the team's own AcmeEvent, to be handed on. */
export function event(
  category: AcmeCategory,
  action: AcmeAction,
  label?: AcmeLabel,
  property?: AcmeProperty,
  value?: number,
): AcmeEvent {
  return acmeEvent({ category: category, action: action, label: label, property: property, value: value });
}
```

`plan.combinations` can list every planned event in the same shape. The
fixture's Swift `files` entry `{language: swift, template:
templates/planned.swift.mustache, file: "AcmePlanned.swift"}` writes:

```text
{{header}}

/// Every planned `{{event_type.raw}}` combination, spelled with the constants of
/// {{namespace}}.swift: an event the plan does not list is not here.
public enum AcmePlanned {
    public static let all: [{{vars.event_class}}] = [
{{#plan.combinations}}
        {{namespace}}.{{function.name}}({{#values}}{{field.names.camel}}: {{vars.prefix}}{{field.names.pascal}}.{{names.camel}}{{^last}}, {{/last}}{{/values}}),{{#deprecated}} // deprecated{{/deprecated}}
{{/plan.combinations}}
    ]
}
```

```swift
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: se (structured). Regenerate with `tripl codegen`.

/// Every planned `se` combination, spelled with the constants of
/// AcmeStructured.swift: an event the plan does not list is not here.
public enum AcmePlanned {
    public static let all: [AcmeEvent] = [
        AcmeStructured.event(category: AcmeCategory.checkout, action: AcmeAction.open, label: AcmeLabel.paywall, property: AcmeProperty.class_),
        AcmeStructured.event(category: AcmeCategory.checkout, action: AcmeAction.tap, label: AcmeLabel.payButton),
        AcmeStructured.event(category: AcmeCategory.home, action: AcmeAction.open, label: AcmeLabel.default_),
        AcmeStructured.event(category: AcmeCategory.home, action: AcmeAction.tap, label: AcmeLabel.promoSheet), // deprecated
        AcmeStructured.event(category: AcmeCategory.home, action: AcmeAction.tap, label: AcmeLabel.promoSheet2),
        AcmeStructured.event(category: AcmeCategory.home, action: AcmeAction.view, label: AcmeLabel._1stCard),
        AcmeStructured.event(category: AcmeCategory.paywall, action: AcmeAction.close, label: AcmeLabel.offer),
        AcmeStructured.event(category: AcmeCategory.paywall, action: AcmeAction.show, label: AcmeLabel.offer),
    ]
}
```

##### Screens as planned (type, id) pairs {#recipe-screen-pairs}

A `screen_view` event type whose screens are one enum of the planned
(type, id) pairs, so that a pair the plan does not list does not compile. The
template reads each field's value of a combination through `value.<field>`:

```yaml
event_types:
  screen:
    codegen:
      style: screen_view
      transport:
        kotlin: {function: "AcmeTracker.trackScreen", args: {type: type, id: id}}
        swift: {function: "AcmeTracker.shared.trackScreen", args: {type: type, id: id}}
        ts:
          function: "acmeTracker.trackScreen"
          positional: [type, id]
          import: "import { acmeTracker } from './acmeTracker';"
      type_names: {namespace: AcmeScreens, function: screen}
      template:
        kotlin: templates/screen.kotlin.mustache
        swift: templates/screen.swift.mustache
        ts: templates/screen.ts.mustache
```

`templates/screen.kotlin.mustache`:

```text
{{header}}
{{#kotlin_package}}

package {{kotlin_package}}
{{/kotlin_package}}

/** The planned `{{event_type.raw}}` screens: a (type, id) pair the plan does not list does not compile. */
enum class {{vars.prefix}}Screen(val type: String, val id: String?) {
{{#plan.combinations}}
    {{names.upper_snake}}({{value.type.literal}}, {{#value.id}}{{literal}}{{/value.id}}{{^value.id}}null{{/value.id}}){{#last}};{{/last}}{{^last}},{{/last}}
{{/plan.combinations}}
}

object {{namespace}} {
    fun {{function.name}}(screen: {{vars.prefix}}Screen) {
        {{transport.function}}({{#transport.args}}{{label}} = screen.{{names.camel}}{{^last}}, {{/last}}{{/transport.args}})
    }
}
```

`AcmeScreens.kt`:

```kotlin
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: screen (screen_view). Regenerate with `tripl codegen`.

package com.example.acme

/** The planned `screen` screens: a (type, id) pair the plan does not list does not compile. */
enum class AcmeScreen(val type: String, val id: String?) {
    CHECKOUT_CART("checkout", "cart"),
    CHECKOUT_CONFIRM("checkout", "confirm"),
    HOME("home", "main"),
    PAYWALL_ANNUAL("paywall", "annual"),
    PAYWALL_MONTHLY("paywall", "monthly"),
    SETTINGS("settings", null);
}

object AcmeScreens {
    fun screen(screen: AcmeScreen) {
        AcmeTracker.trackScreen(type = screen.type, id = screen.id)
    }
}
```

In TypeScript, the same pairs are a `const` object:

```ts
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: screen (screen_view). Regenerate with `tripl codegen`.

import { acmeTracker } from './acmeTracker';

/** The planned `screen` screens: a (type, id) pair the plan does not list does not type-check. */
export const AcmeScreens = {
  checkoutCart: { type: 'checkout', id: 'cart' },
  checkoutConfirm: { type: 'checkout', id: 'confirm' },
  home: { type: 'home', id: 'main' },
  paywallAnnual: { type: 'paywall', id: 'annual' },
  paywallMonthly: { type: 'paywall', id: 'monthly' },
  settings: { type: 'settings', id: null },
} as const;
export type AcmeScreen = (typeof AcmeScreens)[keyof typeof AcmeScreens];

export function screen(screen: AcmeScreen): void {
  acmeTracker.trackScreen(screen.type, screen.id);
}
```

##### Event-name and property-key constants {#recipe-named-constants}

A `named` event type as plain constants: the event names exactly as the plan
spells them (a function for a name with a `${token}`) and the property keys.
The event type's own `vars` override the prefix:

```yaml
event_types:
  app:
    codegen:
      style: named
      vars: {prefix: AcmeApp}
      type_names: {namespace: AcmeAppConstants}
      template:
        kotlin: templates/named.kotlin.mustache
        swift: templates/named.swift.mustache
        ts: templates/named.ts.mustache
```

`templates/named.kotlin.mustache`:

```text
{{header}}
{{#kotlin_package}}

package {{kotlin_package}}
{{/kotlin_package}}

/** The `{{event_type.raw}}` event names, exactly as the plan spells them. */
object {{vars.prefix}}EventNames {
{{#plan.events}}
{{#deprecated}}
    @Deprecated("Deprecated in the tracking plan.")
{{/deprecated}}
{{^dynamic}}
    const val {{names.upper_snake}} = {{literal}}
{{/dynamic}}
{{#dynamic}}
    fun {{names.camel}}({{#tokens}}{{names.camel}}: String{{^last}}, {{/last}}{{/tokens}}): String = {{name_expr}}
{{/dynamic}}
{{/plan.events}}
}

/** The `{{event_type.raw}}` property keys. */
object {{vars.prefix}}EventKeys {
{{#plan.fields}}
    const val {{names.upper_snake}} = {{literal}}
{{/plan.fields}}
}
```

`AcmeAppConstants.kt`:

```kotlin
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: app (named). Regenerate with `tripl codegen`.

package com.example.acme

/** The `app` event names, exactly as the plan spells them. */
object AcmeAppEventNames {
    const val CHECKOUT_STARTED = "checkout_started"
    @Deprecated("Deprecated in the tracking plan.")
    const val CLASS = "class"
    const val PAYWALL_SHOWN = "paywall_shown"
    fun promoPromoCodeApplied(promoCode: String): String = "promo_${promoCode}_applied"
}

/** The `app` property keys. */
object AcmeAppEventKeys {
    const val IS_TRIAL = "is_trial"
    const val PLAN_TIER = "plan_tier"
    const val SOURCE = "source"
}
```

In Swift:

```swift
// Generated by tripl codegen — do not edit.
// Plan: project acme, branch main, plan a1b2c3d4e5f6.
// Event type: app (named). Regenerate with `tripl codegen`.

/// The `app` event names, exactly as the plan spells them.
public enum AcmeAppEventNames {
    public static let checkoutStarted = "checkout_started"
    @available(*, deprecated, message: "Deprecated in the tracking plan.")
    public static let class_ = "class"
    public static let paywallShown = "paywall_shown"
    public static func promoPromoCodeApplied(promoCode: String) -> String { "promo_\(promoCode)_applied" }
}

/// The `app` property keys.
public enum AcmeAppEventKeys {
    public static let isTrial = "is_trial"
    public static let planTier = "plan_tier"
    public static let source = "source"
}
```

## `--check` in CI {#check-in-ci}

Generated files are committed like any other source, so a plan change shows up
as a diff in review. `tripl codegen --check` renders everything in memory,
compares it with the files on disk, writes nothing, and **exits 1** when any
file is missing, differs or is stale, naming each one. Run it in CI to catch a
plan change nobody regenerated for:

```yaml
# .github/workflows/tracking-plan.yml
name: Tracking plan

on:
  pull_request:

permissions:
  contents: read

jobs:
  tripl-codegen:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6

      - name: Generated tracking code matches the plan
        env:
          TRIPL_BASE_URL: https://tripl.example.com
          TRIPL_API_KEY: ${{ secrets.TRIPL_READ_ONLY_KEY }}
        run: uvx tripl codegen --check
```

Notes:

- With `codegen.out` in the configuration, one command checks every language.
  Without it, pass `--out`, or run once per language with `--lang` and `--out`.
- A read-only `tk_r_` key is enough. If you keep a write key for
  [`tripl annotate`](../run/cli.md#in-a-github-actions-workflow), do not reuse it.
- The check compares against the **main** plan unless you pass `--branch` (or
  set `branch` in the file). A plan change on a branch fails the check once the
  branch merges, which is the moment the code has to follow.
- The header carries the plan's content hash, not its revision, so a new
  revision alone does not fail the check. A content change does change the
  hash, and with it every generated file's header, even when the change does
  not touch that file's event type. Regenerate and commit; the diff below the
  header is what to review.
- Add the `tripl check` job from [its guide](./tripl-check.md#github-actions)
  next to this one. Codegen covers the calls that use generated code; the check
  still covers the ones that do not.

## JSON Schema export {#json-schema-export}

The same plan is available as **JSON Schema** (draft 2020-12), one schema per
event, for validators and code generators outside tripl (ajv in a test, a
schema registry, quicktype):

- `tripl export --format jsonschema --out DIR` writes the bundle. See
  [`tripl export`](../run/cli.md#tripl-export).
- **Export JSON Schema** on the **Plan history** page downloads it for the
  branch you are viewing.
- The API is [`GET /projects/{slug}/plan/export?format=jsonschema`](./agent-api-guide.md#plan-export).

Each schema lists the event type's fields as properties, with required fields in
`required`, the event's own values as `const`, enums from the field's options or
the variable's allowed values, and `pattern`, `minimum` and `maximum` from the
field's contract. The [API guide](./agent-api-guide.md#plan-export-jsonschema)
has the full mapping.

## Related pages {#related}

- [`tripl codegen`](../run/cli.md#tripl-codegen) and
  [`tripl export`](../run/cli.md#tripl-export) on the CLI page: every flag.
- [Check code against the plan](./tripl-check.md): the configuration file
  this page extends, and the checker that covers calls the generated code does
  not.
- [Agent API guide: plan export](./agent-api-guide.md#plan-export): the endpoint
  behind both commands, including the `codegen_model` format.
- [Variables & templates](../use/variables-and-templates.md): documented values
  and `${variable}` placeholders, which become enums in generated code.
