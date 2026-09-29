---
title: Properties & templates
sidebar_position: 4
---

# Properties & templates

Properties keep reusable values and warehouse paths consistent across the
tracking plan. An event value can contain a placeholder such as
`${checkout_variant}` instead of copying a changing list or source path into
every event.

Properties are tripl's typed event properties: the keys an event's JSON carries,
each with a type, a description and documented values. Until F23 they were
called **variables**. The API still accepts the former `/variables` paths as a
deprecated alias for one release, and `tripl plan variables` still works as a
second name for `tripl plan properties`.

Use **Plan → Properties** to create and review them. Properties are part of the
active plan branch, so edits follow the same review and merge workflow as
events, fields, and relations.

## What a property stores

Each property has:

- a stable, lower-case **name** used by `${name}` placeholders;
- a **type** (`string`, `number`, `boolean`, `date`, `datetime`, `json`, or an
  array type), optionally refined by a **JSON Schema** fragment;
- a human-readable **description**;
- optional **documented values** — the global list the team expects;
- optional **bindings** — warehouse columns or dotted JSON paths such as
  `variant` or `page_data.extra.variant`;
- observed contexts and samples discovered by scans;
- optional **per-event documented-value overrides**.

The name is for people and templates. A binding is how a scan recognizes the
same concept in raw data. Keeping those separate lets a scan turn a long source
path into a short, readable `${variant}` placeholder without losing the source
mapping.

Only the name and type are required. **You do not have to fill in bindings** —
a scan matches a property by its name first, so a property named after the
column it stands for needs no binding at all.

Each token belongs to one property. Creating or editing a property is refused
with a conflict when a new binding is already another property's name, binding
or scan source, and when a new or changed name is already another property's
binding or scan source (a property renamed after a scan keeps its original
source). Editing a property does not re-check the bindings it already has, so
scan-created bindings such as `props.$os` and names such as `userId` save
unchanged; only newly added bindings must be a column or dotted path.

A scan skips, and reports in the run details, any property token longer than
100 characters, such as a JSON key typed by a user.

### Refine the type with JSON Schema

The type is the coarse kind. The optional `json_schema` field says more, in
JSON Schema terms. For example, a number can be narrowed to an integer, and a
string can get a format. An array can declare its item type, and a `json`
property can describe the keys it carries.

```json
{
  "type": "object",
  "properties": {
    "total": { "type": "number", "minimum": 0 },
    "coupon": { "type": "string", "enum": ["SPRING", "VIP"] }
  },
  "required": ["total"]
}
```

A nested object is one property with a sub-schema, not a set of dotted
properties.

**Supported keywords.**

- Every node: `type` (one of `string`, `number`, `integer`, `boolean`, `array`,
  `object`) and `description`.
- `string`: `format`, `pattern`, `minLength` and `maxLength`.
- `number` and `integer`: `minimum`, `maximum`, `exclusiveMinimum`,
  `exclusiveMaximum` and `multipleOf`.
- `array`: `items`, `minItems`, `maxItems` and `uniqueItems`.
- `object`: `properties`, `required` and `additionalProperties` (a boolean).

**What is refused.** Anything else is refused by name: `$ref`, combinators,
and a list of types. The same goes for an `enum` at the top level, because
documented values already hold that list. An `enum` inside a nested property is
fine.

**The schema must agree with the type.**

| Type | Schema |
|---|---|
| `string` | `string`. Date formats are refused: use the `date` or `datetime` type. |
| `number` | `number` or `integer` |
| `boolean` | `boolean` |
| `date` | `string` with `format: date` |
| `datetime` | `string` with `format: date-time` |
| `json` | `object` or `array` |
| `string_array` | `array` with `items` of type `string` |
| `number_array` | `array` with `items` of type `number` or `integer` |

**When the pair disagrees.**

- A change to either half that would break agreement is refused. Send both
  halves in one request, or set `json_schema` to `null` to clear it.
- A bulk type change is refused as a whole if any selected property has a
  schema the new type contradicts.

**Types a scan infers.** When a scan collects the first sample values for a
JSON-path property it created, it sets the property's type from the JSON kind
of those values:

| Sample values | Type set |
|---|---|
| Numbers | `number`; narrow it to `integer` yourself if that is what it is |
| `true` / `false` | `boolean` |
| ISO dates | `date` |
| ISO date-times, or date-times mixed with dates | `datetime` |
| Arrays of strings | `string_array` |
| Arrays of numbers | `number_array` |
| Any other arrays, or objects | `json` |

- A scan types a property only once, and only while it is untouched: it still
  has the default `string` type, no schema, and the scan's own description.
  After that the scan never changes the type.
- A sample that mixes kinds, such as `"42"` next to `42`, sets no type.
- A sample of only nulls or only empty arrays sets no type either, so a later
  sample can still set one.
- A property whose values were already recorded before this feature keeps
  `string` until you set its type yourself.
- A schema the scan wrote does not count as your edit, so the property can
  still be retired automatically.

### A binding and a `${token}` are not the same thing

They are written the same way and they are frequently the same string, which is
why the question comes up. They are still two different things:

- a **binding** is an address in the warehouse — a column, or a dotted path
  inside one. It tells a scan where to read.
- a **`${token}`** is a property's **name**. It is what the plan references, and
  it is what the suggestion list offers as you type `$` in a field value.

They coincide on a property a scan created, by construction: the scan stores the
path it found as the binding, and derives a short name from its trailing
segments — `variant`, then `extra_variant`, then `page_data_extra_variant` —
falling back to the **raw path as the name** when every short candidate is
already taken. That is why a mature project can be full of properties literally
named `property.forecast_profile`, whose token is that same dotted string.

On a property you create yourself they differ on purpose: name it `variant`,
bind it to `page_data.extra.variant`, and write `${variant}`.

The example under the **Data bindings** field is taken from a property in your
own project when there is one to take it from, so the shape it shows is the
shape your warehouse actually uses. Note also that nothing checks a binding
against the warehouse: a path with a typo is accepted, and the only symptom is
that the property never collects an observed value.

## Documented, observed, and effective values

tripl deliberately keeps two kinds of value list separate:

- **Documented values** are authored by the team and express the contract.
  Scans never rewrite them.
- **Observed values** and counts are evidence collected from the warehouse.
  Low-cardinality contexts retain the observed list; high-cardinality contexts
  show bounded examples and an observation count. A plain column's count is an
  exact distinct count over the scanned window, but a JSON path's comes from a
  capped sample and is a floor — read it as "at least this many", and expect a
  JSON path to be high-cardinality however few values it returned. Samples
  **accumulate**: re-sampling a path merges what the run saw into the stored
  list instead of replacing it, so a value observed once stays on record even
  when later scan windows no longer contain it. An exact enumeration stays
  exact: a low-cardinality context keeps every value through a merge until the
  union outgrows the cardinality threshold, and only then does it stop keeping
  the full list and behave as high-cardinality — whose stored list is a capped
  sample — from then on.

### The other store: an event's own field value

Accumulation is a property of observed values, and **only** of them. An event's
plain field value is a single string: one value per field, per event, full stop.
That matters because a scan can produce several warehouse rows for one event —
one per breakdown combination — and they collapse onto that one field.

The rule is **the busiest row wins**. Rows are applied in ascending order of
volume, so the highest-count row for an identity is written last and its value
is the one stored. A rare row cannot overwrite the common case: an event seen
12,000 times on `home` and three times on `purchase/main` keeps `home`.

Nothing is merged, and nothing warns you in the plan itself — so when a field
did see more than one value, the **scan report says so**, naming the field and
how many values it saw (`windbar_tap.screen (3 values)`). Read that as "this
field varies across the rows behind this event"; the stored value is the
dominant one, not the only one.

Ordering between events is unaffected: identities keep first-appearance order,
so the sort changes which value survives, never which events exist or in what
order they were created.

### Clearing what a scan has recorded

**Plan › Properties › open a property › Observed › Clear observed values** (the
quick-edit dialog has it too) drops that
property's contexts and keeps everything else on the row — description,
documented values, bindings, per-event overrides, and every drift verdict. It
is the reset that previously required deleting the whole property, which took
all of that with it.

Two consequences worth knowing before you use it. A later scan re-records a
context only where an event field still refers to the property, so a context
whose event has moved on does not come back. And because "has observed values"
is one of the reasons the retirement sweep keeps a property, clearing them can
make an otherwise unreferenced property retirable — the next scan's cleanup may
then remove it.

A **context** is one (property, event, field) pairing — the record that this
event's field refers to this property through that binding. The context and the
values are separate facts, and the context comes first: it exists as soon as
something matches the property to the field, whether or not any value has been
stored into it. So an empty context is a real state, not a missing one, and the
UI names it rather than showing a blank. (On the Properties table the column
listing the events a property was seen in is **Observed in**, and each type chip
shows the schema key — `string`, `number_array` — rather than a prose label.)
The Properties table says **No values
stored** for a property that has contexts but no samples, and an event's value
popover distinguishes a context that holds no value from one whose values were
counted without an example being kept. Each popover line speaks for its own
event and field, not for every context of the same binding.

An empty context is not by itself a fault. A binding pointed at a column that is
genuinely empty stays empty however often it is looked at. A JSON-path binding
has a second, temporary reason to be empty: a scheduled run samples the paths
still waiting for their first values a slice at a time, and the rotation reaches
every waiting path every few runs — so on a regularly collecting scan, a newly
referenced path normally shows its first values within hours. Only paths with a
context to fill are in that rotation: a property whose token no event value
references has no context row for a sample to land in and is not sampled at
all, and a property a scan has just created becomes sampleable one scheduled
run after something references it.

The global documented list applies everywhere unless an event has an override.
A per-event override **replaces** the global list for that event; it is not
merged with it. This makes exceptions explicit — for example, most events may
allow `control` and `treatment`, while one legacy event documents a different
set.

To add an override, edit the property, choose an event under **Per-event
overrides**, enter the complete effective list for that event, and save it. The
event list is one searched page rather than the whole catalog, so on a large
project type into the search box above it to reach the event you want — the note
under the list says how many events it is not currently showing.

### An event's property list

Each event can list the properties it carries. Every entry
records two things:

- whether the property is **required**, meaning every occurrence of the event
  carries it;
- optionally, an override of its allowed values.

An override is one kind of entry. So a property with an override on an event is
also on that event's list, and every override from before this feature appears
in the list as an optional property.

An entry without an override uses the property's documented list. The editor's
**Per-event overrides** section shows only entries that have their own values.
Deleting an override from a required property keeps the property in the list
and only removes its values.

The list is read with `GET /events/{event_id}/properties`. Each entry names its
property with the property's type, schema and description, and gives the
effective values. Entries are written through the event-overrides endpoint.
That endpoint works as a patch:

- `{"required": true}` adds the property, or marks it required;
- `{"values": [...]}` sets the override;
- `{"values": null}` removes the override and keeps the entry;
- `DELETE` removes the entry.

Scans do not write the list. They record what they observe instead.

**How a scan records JSON keys.**

- When several scanned rows collapse into one event, the event's JSON value
  carries every key any of those rows had. Earlier, only the busiest row's keys
  were kept, so optional properties disappeared.
- A kept literal value that differs between rows still comes from the busiest
  row.
- For each JSON-path property, the scan measures a **presence rate**: the share
  of the event's rows, weighted by their counts, that carried the key. It is
  stored with the observed values.
- `GET /events/{event_id}/properties` returns the rate as `presence_rate`. It
  is `null` until a scan that returns row counts has measured it.

### Property drift

Scans compare what they see with each event's property list, and report
three kinds of **property drift**:

| Kind | Reported when | Accepting it |
|---|---|---|
| `new_property` | The event carried a JSON key whose property is not on its list. Only reported for events whose list names at least one property. | Adds the property to the event's list as an optional property. |
| `missing_required` | A required property was carried less often than the event's threshold, including never. | Makes the property optional. |
| `type_change` | Sample values have a type the property's type does not allow. This is reported per property, with no event. | Changes the property to the observed type. |

- **Threshold.** Each event has a presence threshold,
  `required_presence_threshold`. By default it is 0.95. Set it with
  `PATCH /events/{event_id}`. Like the rest of the event, it is part of the
  plan's branches.
- **`suggested_required`.** `GET /events/{event_id}/properties` includes this
  field. It says whether the measured presence reaches the threshold. It is
  only a suggestion: `required` is only ever set by a person.
- **Which properties get a `type_change`.** A scan-created property that still
  has the default `string` type is not checked.
- **Allowed variations.**
  - A `string` may hold dates.
  - A `datetime` may be sampled as a bare date.
  - `json` accepts arrays.

Triage works like value drift: accept, snooze, mark as a false positive, or
reopen.

- An accepted drift reopens if the scan sees it again.
- An open drift with no note disappears once a later scan no longer finds it,
  for example when the property is back above the threshold or you added it to
  the list yourself.
- Archived events and properties excluded from scans are not checked.
- Drift is kept for 30 days.

```text
GET   /api/v1/projects/{slug}/properties/property-drifts?event_id=&variable_id=&kind=&active_only=
POST  /api/v1/projects/{slug}/properties/property-drifts/{drift_id}/action
```

## Bind a property to warehouse data

Skip this when the property's name already matches the column — a binding earns
its keep only when the two are spelled differently, such as
`page_data.extra.variant` behind `${variant}`. In that one case, leaving it
empty costs you quietly and later: the next scan does not recognize your
property, mints a second one beside it (`extra_variant`, say), and the one you
made by hand collects no contexts, ever. It will not show up under **Unused**
either — a property you described is a property you claimed.

On a property a scan created, the binding it filled in is how it keeps finding
that property. Clearing it marks the property as hand-owned, which permanently
exempts it from the retirement sweep.

Bindings accept a scalar column name or dotted JSON path:

```text
experiment_variant
page_data.extra.variant
```

When a scan sees a matching source path, it adopts the existing property rather
than creating a second scan-named property. New scan-created properties receive a
short display name where possible while retaining the raw source path as their
binding. Search on the Properties page matches that source path and the bindings
as well as the name and description, so a property shortened to `${aalter}` is
still found by searching for the `property.Aalter` it binds to.

Binding rules:

- start with a letter or underscore;
- use letters, digits, `_`, or `-` in each segment;
- separate JSON path segments with `.`;
- do not add the same binding twice.

## Use placeholders in event values

Event field and meta values can contain `${variable_name}`. The event editor
offers matching properties as you type and previews their description, bindings,
and documented values. Long detail lines stay inside the picker on narrow
editors and are shortened visually rather than expanding the page. Unknown
tokens are highlighted before save; the API also returns advisory `warnings` on
event create/update responses.

Example:

```text
${checkout_variant}
```

Placeholders are a documentation contract, not a runtime expression language:
tripl stores the template and uses it to relate plan values to observed property
contexts. It does not substitute a single global value into the event.

Renaming a property brings its references with it. Every `${old_name}` stored on
an event in the same branch is rewritten to `${new_name}` as the rename is
saved, and both `${token}` sites are covered: an event's **field values** and
its **meta values**.

Hand-authored event field values are protected from scheduled scans. A scan can
still add a missing value, but it will not overwrite a value that a user saved
through the event API or UI.

## Review value drift

After a scan, tripl compares observed values with the effective documented list
for each event. Novel values create a **property value drift**. Open drift counts
appear on the Properties table, and the same review panel is available on the
affected event's detail page.

The comparison has nothing to say about a context holding no values. No drift on
a property therefore means either "everything seen was documented" or "nothing
was seen" — read the property's observed column to tell those apart, because
only the first is evidence that the contract holds.

It also has no sense of when a value first appeared: it weighs everything the
context currently holds, not only what arrived since the last scan. So the first
scan after you document a list on a property that has been observed for a while
reports every value already seen that falls outside it, usually as one batch.
That is intended, not a fault. Until the list existed there was no contract, so
none of those values had ever been judged, and passing over them silently would
hide the very history you wrote the list to rule on. Work the batch as ordinary
drift: **Accept globally** on each row that turns out to be legitimate folds its
values into the documented list, and what stays open is the part worth
investigating.

Available actions:

- **Accept globally** — add the novel values to the property's global
  documented list.
- **Accept for this event** — create or update the event override, seeded from
  the current effective list.
- **Snooze** — hide the drift until a chosen time, which has to be in the
  future, while scans continue to refresh its evidence.
- **False positive** — resolve it without changing the documented contract.
- **Reopen** — return a resolved drift to active review. On a row that is only
  snoozed the same control reads **Un-snooze**, because a snooze is what the
  click undoes; both send the same action, which clears the snooze as well as
  any resolution.

Drift that is not asking for attention is collapsed, not hidden. Both panels
carry a toggle that names what it is holding — **Show N resolved**, **Show N
snoozed**, or **Show N snoozed or resolved** when the group has both — so an
acceptance you regret can always be reopened and a snooze can always be ended
early. A snooze also comes back on its own once its time is up, whether or not
the page was reloaded in between.

A later scan **reopens an accepted drift by itself** as soon as it observes a
value the documented list does not cover. Accepting is what puts the values in
that list, so a value you accepted does not come back and "outside the accepted
set" means genuinely new — the reopened row shows only the new values, and alert
rules subscribed to property value drift see it again. Snoozed and
false-positive rows are never reopened by a scan.

The documented list is the arbiter, not the row's own history: if you later
**remove an accepted value from the documented list by hand**, the next scan
that sees it opens a drift again. A resolved row cannot keep vouching for a
value the plan no longer documents — and this is also what stops a row that
silently absorbed values under an older build from suppressing them forever.

Alert rules can opt into **Property value drift**. These candidates behave like
other drift signals: they use the spike direction for rule matching, carry the
property name and novel-value sample in the alert, and bypass numeric volume
thresholds.

The scope produces nothing until some property documents values, because drift
is measured against a documented list and there is nothing to compare an
observation with until one exists. A global documented list or a per-event
override will do — either one is enough — but it has to be on the **main**
branch: detection runs against main, so a list documented on a working branch
counts only once that branch merges. A property
[excluded from scans](#exclude-instead-of-deleting-scan-owned-properties) never
drifts however full its list is, because scans stop observing it. The rule
editor and the monitor detail now say so where the scope is switched on, rather
than leaving a rule to look enabled and stay silent — see
[When a scope is on but nothing feeds it](alerting.md#when-a-scope-is-on-but-nothing-feeds-it).

## When a scan merges events into a group

Scan **event group** rules can fold several existing events into one. The
surviving event keeps the property data of the events it absorbed: observed
contexts, per-event documented-value overrides, and value-drift triage all move
across rather than disappearing with the merged-away event.

Two details worth knowing:

- a context moves only when the surviving event's value for that field still
  names the property. A group rule that rewrites a field value to the pattern it
  matched removes the reference, so the context is dropped rather than left
  asserting a reference that is no longer there. A JSON column is never
  rewritten that way: a rule condition on the column itself still groups the
  event, but its value keeps the template, so every `${column.path}` context
  moves with it;
- where both events already carried an entry for the same property, the
  surviving event's own override or drift decision wins. Observed contexts are
  combined instead: the observation count becomes the number of distinct values
  across both sides (never less than the larger of the two counts), and the
  values are unioned. A low-cardinality context keeps every value until the
  union outgrows the cardinality threshold; it then becomes high-cardinality and
  its values are sampled.

## Exclude instead of deleting scan-owned properties

Deleting a property removes it from the plan, but a later scan can discover the
same bound source path and create it again. Use **Exclude from scans** when the
intent is “this source value must stay out of the plan.”

Exclusion keeps a lightweight tombstone:

- the property moves to the **Excluded from scans** section;
- scans do not recreate it or accumulate new contexts/drift for it;
- **Restore** makes it active again;
- permanent delete remains available when no scan can reintroduce it.

## Unreferenced scan-created properties are retired automatically

A scan creates a property for every placeholder it detects. On a JSON column
whose keys are user-typed text — a map rather than a struct — that once meant a
permanent plan row per key. A catalog run now ends by deleting the scan-created
properties that nothing refers to any more. **Which runs do that, and over
which properties, is a shorter list than "all of them":**

| Run | Retires unused properties? |
| --- | --- |
| A scan you start by hand | Always for JSON-path properties; for scalar-column properties, only when every scan config in the project declares **Limits → Lookback (hours)** |
| A **scheduled monitoring collection** | Always for JSON-path properties; for scalar-column properties, only when every scan config in the project declares **Limits → Lookback (hours)** |
| A **metrics replay** | Never |

Both exceptions are the same rule seen twice: a run only decides a property is
unused from a view it can defend.

A manual scan with no lookback reads everything its base query returns, but the
property sweep spans the whole project. A sibling config may have rewritten a
scalar property using a narrow collection interval, so the manual scan alone
cannot justify retiring it. A scheduled
collection has no such view. It always reads through a window, and with
**Lookback (hours)** left blank that window is the slice it is collecting —
usually one or two intervals, often a single hour. What that narrow view can do
to a property depends on where the property came from.

A property minted from a **scalar column** stands, as `${token}`, in that
column's value on every event of the type. A column carrying thousands of
values across your table can carry a handful in one hour, and a run that sees a
handful stores those values *literally* in place of the `${token}` template —
in every event at once. That rewrite is what loses the field's observed-value
history, and it happens with or without a sweep; what a sweep would add is
deleting the property row, so that the column's next busy hour mints it again
under a new id. Setting a lookback is you saying which window represents your
tracking plan; for these properties retirement runs behind that statement and
not ahead of it.

A property minted from a **path inside a JSON column** — its scan source path is
`column.path`, and `column` is a JSON field of the event type — has no such
rewrite to follow. A key missing from one hour's rows drops out of that one
event's stored value, not out of every event, and a key nothing refers to any
more is exactly what the sweep exists to remove. So a scheduled collection
judges these on every run, lookback or not. When the key arrives again the next
run mints the property again — a new row with a new id, so anything that stored
the old id (an agent's cache, a bookmark) stops resolving.

A **metrics replay** never retires anything, for the older reason: it does not
sync the catalog at all — it recomputes counts over a past window and creates no
events or properties — so it never sees which paths your rows currently carry and
is in no position to call a property unused.

:::warning Scalar-column retirement needs lookbacks on every project scan
The create page pre-fills **Limits → Lookback (hours)** with 24, but a config
saved without one shows the field blank, and blank is a legitimate setting: each
run reads the whole base query. It also means catalog runs across that project
judge only the properties minted from JSON paths
— the shape that grows a permanent row per key, a map keyed by free text
collected hourly, is swept — and leave every property minted from a scalar
column alone. If those are the rows piling up, set a representative lookback on
every project scan config or clear the backlog from the
danger zone below. *Properties retired* on a
[run](./feature-reference.md#scan-runs) is present, `0` included, on every
manual run and every scheduled collection, and absent only on a replay — so on a
scheduled run with a blank lookback, `0` means the JSON-path properties were
looked at and found in use, not that the scalar ones were.
:::

Retirement works on `main`, where scans write; the copies on an open working
branch are left alone.

A property is retired only when **all** of the following are true:

- a scan created it and its description is still the scan's own
  (*Auto-detected property from data source scan*);
- its display name is still one the scan itself could have given it — the scan
  names a discovered path by shortening it (`property.session_time` becomes
  `session_time`), so a name outside that shortening is one you typed;
- its bindings are still only the source path the scan gave it;
- it documents no values and carries no per-event override;
- it carries no value drift — open or resolved;
- no scan-observed context is recorded against it;
- nothing stored in the project mentions any of its tokens — its display name,
  its scan source path, or any binding — as `${token}`. Both `${token}` sites
  count: an event's **field values** and its **meta values**.

Everything a person touched is out of reach. A name you typed, an edited
description, a binding you added, documented values, an override, a drift you
accepted or snoozed, and an **Exclude from scans** tombstone each keep the row. So does a single
`${token}` left in one event value, even when the property has no observed
contexts at all.

The one gap in that list is narrow and worth knowing: the name check asks
whether the scan *could* have chosen your name, not whether it did. Rename
`session_time` to `property_session_time` — another shortening of the same path
— and the row reads as the scan's own. Rename it to anything else, which is
what a rename is usually for, and it is yours.

The run that creates a property does not normally retire it in the same pass:
that run writes the property's token into at least one event's field value, so
the reference check keeps it. What retirement removes is the row whose token has
since vanished from every stored value — the leftover of a key that stopped
arriving, or of an event value that was edited to stop using it.

One case does create and retire in the same run: a path that appears only on an
**archived** event. A scan deliberately leaves an archived row's field values
untouched, so the token is never written and nothing live refers to the new
property.

When a run retires anything it says so in the run's details list: *Retired N
unused properties no event refers to*. To see the set for yourself, the Properties
table's **All / In use / Unused** filter asks the server the same question:
**Unused** lists exactly the rows a run would take, decided by the rule above
rather than by a "used in no events" count.

:::note Clearing a backlog that predates the sweep
An organization owner or admin can run the same pass over a whole branch on demand, from
**Retire unused properties** in the project's [danger
zone](./feature-reference.md#project-general--danger-zone) — **Preview** first,
which commits nothing and reports what it would take, then **Retire**. The route
behind it is `POST /api/v1/projects/{slug}/danger/retire-unused-variables`.
:::

## Bulk changes and branches

Select properties in the table to change their type or description, add
documented values, or delete several at once. Bulk operations apply a uniform
patch to the selection; they do not replace bindings or per-event overrides.

Properties, bindings, schemas, documented values, overrides, exclusions, and
drift-related plan changes are branch-aware. A merge or a revert treats the type
and the schema as one value, so they never end up taken from different sides. The merge dialog warns when a branch would delete
a property that still exists on `main`, so reviewers can catch a destructive
change before it lands.

## API endpoints

The interactive [API reference](../integrate/api) is authoritative. The main
workflow uses:

```text
GET/POST             /api/v1/projects/{slug}/properties
PATCH/DELETE         /api/v1/projects/{slug}/properties/{variable_id}
POST                 /api/v1/projects/{slug}/properties/bulk-update
POST                 /api/v1/projects/{slug}/properties/bulk-delete
GET                  /api/v1/projects/{slug}/properties/{variable_id}/values
GET                  /api/v1/projects/{slug}/properties/{variable_id}/event-overrides
PUT/DELETE           /api/v1/projects/{slug}/properties/{variable_id}/event-overrides/{event_id}
GET                  /api/v1/projects/{slug}/events/{event_id}/properties
GET                  /api/v1/projects/{slug}/properties/drifts
POST                 /api/v1/projects/{slug}/properties/drifts/{drift_id}/action
```

Pass the current `branch` query parameter on plan-scoped calls.

## Related pages

- [Concepts](./concepts.md)
- [User guide](./user-guide.md)
- [Feature reference](./feature-reference.md)
- [Alerting rules](./alerting.md)
- [Agent API guide](../integrate/agent-api-guide.md)
