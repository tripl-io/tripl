---
title: Feature Reference
sidebar_position: 7
---

# Feature Reference

This page is a complete map of tripl's user-facing surface, organized the way
the app itself is: three job-based areas in the project sidebar — **Plan**,
**Observe**, **Govern** — plus the cross-cutting tools (branch switcher, command
palette, activity feed, AI). Each subsection states what the feature does, how
you reach it, and the key options it exposes.

For the underlying mental model (events vs. event types, scopes, signals) read
[Concepts](./concepts.md) first. For step-by-step walkthroughs see the
[User Guide](./user-guide.md); for fixes see [Troubleshooting](./troubleshooting.md).

:::note Permissions
Mutations (create/update/delete) require at least the **editor** role; viewers
are rejected. Inside a project an editor may also need to own it: a demo, or a
project another editor created, is changed only by its creator or an owner.
Data sources and the workspace/instance settings require the
**owner** role. Read surfaces are available to any signed-in member. Owner-only
command-palette entries (such as **Runtime**) are hidden for non-owners.
:::

## Navigation model

The project sidebar groups every surface into three job-based areas:

| Area | Surfaces |
|------|----------|
| **Plan** | Events (with one entry per event type under it), Event types, Meta fields, Variables, Relations, Plan branches, Plan history |
| **Observe** | Overview, Metrics, Anomalies, Alerting |
| **Govern** | Reconciliation, Coverage, Scans, Audit log |

Above the groups sit the **project switcher**, the **branch switcher** (shown
only inside a project), and the **Search or jump** button (⌘K). The pinned
footer holds **Project settings** and **Concepts** (the in-app domain primer),
then your user row: the whole row opens the **account menu** (Profile,
**Workspace settings**, **Appearance**, **Sign out**); **Appearance** opens as a
popover beside the button that opened it. The top bar's crumbs are links: the
project, then the surfaces under it (**Observe › Alerting › Rules** opens the
Alerting page and then its Rules tab). A nav group (Plan, Observe, Govern) is
not a page, so its crumb stays plain text. While the pages read a
plan branch, a **branch strip** under the top bar names the branch and its
status and offers the way back to main. The top bar's **Activity** button opens
and closes the activity rail. The **Search or jump** palette also finds every
settings section by what it holds ("timezone", "api key", "dark mode") and
offers create actions. Collapsed to an icon rail, the
sidebar keeps the project and branch switchers, Project settings, Concepts and an
account menu, with each icon named in a tooltip. Below 1024px the sidebar is a
drawer opened from the top bar, and below 1600px the activity rail is too. Badge counts come from
the cheap project summary: Events (active events), Event types, Variables,
Anomalies (the open monitoring signals — the same number the Anomalies page
shows — when any are open), and Alerting (open incidents, in solid red, when any
are open). The Plan counts describe main, so they are hidden while a branch is
active. Meta fields, Relations, Metrics, Plan branches, Coverage, Scans, and
Audit log carry no count.

**Keyboard shortcuts.** Letter keys work anywhere outside a text field. **?**
opens the shortcut sheet, which lists them all. **c** presses the page's
**New …** button (New event, New metric), or the create button a page marks as
its own when it is labelled otherwise (**Add rule**, **Add connection**,
**Create key**). **/** focuses the list's search box, and **Ctrl K** (**⌘K** on a
Mac) opens **Search or jump**. **Esc** closes a dialog, menu or drawer.
Inside a project, **g** then a letter, pressed within a second, goes to a page:
**g o** Overview, **g e** Events, **g t** Event types, **g m** Metrics, **g n**
Anomalies, **g a** Alerting, **g s** Scans and **g b** Plan branches. Like the
other letter keys, the sequence is ignored while you type in a field or while a
dialog or menu is open.

**List filters on a phone.** Below 640px, a list page's filter chips fold into
one **Filters (n)** button, n being how many are set. It opens a bottom sheet
holding the chips (and the date pickers beside them), where filters still apply
as you pick them; **Done** closes it. The search box stays in the row.

---

## Plan

### Event catalog

**Where:** Plan › Events (the default project landing surface).

The catalog is a table of plan **events**, split into one tab per **event type**
(plus an "all" view). Each row shows the event name, status, tags, recent volume
and its latest anomaly **signal** state. An event type with open drift gets one
**schema-drift badge** beside the page heading, once per type rather than on
every row of it; the events table on an event type's settings page shows that
type's badge above its toolbar. Under a per-column field or meta filter the
heading's **Total** becomes **Matching**: the matches among the rows checked so
far, as the table footer counts them. Controls include free-text search, status and tag filters, a
"silent since N days" filter, a **Verified** filter (Any / Yes / No,
carried in the URL as `?reviewed=true|false`; **Verified** is a flag of its own,
separate from the **In review** status), per-column field-value
and meta-value filters,
saved views, column visibility, bulk actions, and a per-tab aggregate metrics
chart. The review queue can sort **Busiest first**, collapse similar-name
clusters for group selection, and expand the selection from loaded rows to
**Select all N** matching events before a bulk status/owner/review/delete action.
Per-column field and meta filters count as part of "matching" too, so under one
the button reads **Select all matching**. The selection is cleared when you
switch tab, branch or server filter, but not when you only change the sort; a bulk change that reaches rows off screen,
covers more than 50 events or archives asks first, and a finished status, owner
or reviewed change offers **Undo** in its toast when every changed row was
loaded. Undo restores the values the table showed, and leaves any selection you
have made since alone. Drag-to-reorder is off while the list is sorted **Busiest first**,
because the rows are not in catalog order then.
The **Verified** column is hidden by default in the column picker but is forced
visible on the review tab (`/events/review`). Bulk **Mark as verified** sets the
flag; it does not move an event out of the review queue.

An event whose name is blank renders as *(unnamed event)* rather than as nothing
at all, so the row keeps a click target and an accessible name — the one event
you would most want to clean up used to be the one you could not open. Names with
**empty segments** (`::`, `onboarding:start:`) are a different case: those are
real identities and each empty piece still renders as ∅. The same treatment
follows the name onto the Reconciliation page and the command palette.

A **More** menu on the toolbar holds **Export CSV**. It exports the whole
filtered view rather than the rows scrolled into view so far: the active filters
and sort go to the server, the matching events are paged down from it, and the
file is written with the columns the column picker currently shows. **Signal**,
**Δ · 24h** and **48h** are deliberately left out — those cells come from a
metrics request issued only for the rows on screen, so they do not exist for the
rest of the filtered set, and exporting them blank would read as "no volume"
rather than "not fetched". The menu carried an **Ask AI** entry that was never
built; it has been removed rather than shown as permanently unavailable.

### Event detail & editing

**Where:** click an event row to open its detail, or Plan › Events ›
*(event type)* › **New event** / **Edit**.

An event belongs to an event type, so a project with none says so in place of
the picker and links to creating one.

The event form exposes: **Event type** (required; cannot be changed after
creation); **Name** (e.g. `checkout:completed`); **Title** — an optional
free-text label (up to 500 characters) shown beside the name in lists, the
branch diff and the event page, and searchable, that never takes part in the
identity; **Description** — with a
**Suggest with AI** action that appears when editing an existing event and AI is
enabled; **Status** — one of `draft`, `in_review`, `ready_for_dev`,
`implemented`, `live`, `deprecated`, `archived` (selecting `deprecated` reveals a
**Sunset date** and, when editing an existing event, **Replaced by**); **Owner** — a project member, or none, the same value the
list's bulk bar sets; **Tags**; **Metric breakdowns** (the selected type's scalar
fields and the columns this project's scans collect — `platform` and any
configured breakdown column — plus any other warehouse column typed in by hand;
JSON fields are excluded); **Field values**
(per the event type's schema — boolean/enum selects, a JSON editor for `json`
fields that validates and saves canonical JSON while preserving complete
`${variable}` values, variable-aware text inputs; a `number` field takes a
number or a `${variable}` token, and Save stays disabled while it holds
anything else); and **Meta fields** values. A tag or breakdown column still
sitting in its input when you leave the box or save is added, as if you had
pressed Enter. The **Sunset date** is entered and shown in your local time and
stored as that instant. Changing the event type on a new event keeps the values
of fields the new type has under the same name, and asks first when some values
have nowhere to go.
Once at least three of the type's events have been sampled and 80% of them
share a naming style (`snake_case`, `Title Case`, `a:b` …), the **Name**
placeholder shows one of those names instead of a generic example. A name typed
in another style gets a note under the input, *Other Screen View events look
like "Home Screen View"*, which never blocks saving: the name has to be
whatever the app sends.
For a series of similar events, **Save and add another** creates the current
event, says what it created, and keeps the entered form values in place for the
next one — change what differs and save again. It will not save the same name
twice in a row: an unchanged name is flagged before anything is sent. On an
event type with no scan naming rule, an existing event of the same name is a
warning rather than a refusal. Creating an event confirms with a toast (with
**Open**, or on a branch **View changes**), and on a branch the form says the
event is added to that branch and reaches main when the branch merges. Back on
the Events list after a create — one event or a pasted batch — the list scrolls
to the new rows and marks each **New** for a few seconds.

A **viewer** who opens an event's edit address is taken to the event's page
(its monitoring detail), where the spec and the **Discussion** are readable; the
**New event** and **Add many events** addresses take a viewer to the list. A main
event opened while a branch is active (or a branch event opened on main) is
shown read-only, with a **Switch to …** button in place of **Save**.
**Add many events** sets **Owner** and **Tags** once for every row, and for a
type it cannot fill in bulk offers **Add one at a time** and **Edit &lt;type&gt;
fields**.

The **app version column** is deliberately absent from the breakdown picker, and
adding it by hand is refused: app versions are already collected as their own
version series, so listing the column as a breakdown would collect the same
value twice. A column stored back when the picker still offered it is left
alone — it stays in the list so you can remove it, and collection skips it
either way.

#### Retiring an event

Setting the status to `deprecated` reveals two fields that together answer what a
reader of a retired event needs to know: **Sunset date** — when it stops being
supported — and **Replaced by** — what to send instead. The replacement is picked
from a searchable list of the project's other events; the search runs on the
server, so any event in the catalog is reachable by typing part of its name, and
the picker prints how many matches it is not showing rather than quietly
truncating. An event cannot replace itself.

**Replaced by is documentation and nothing else.** No scan matches through it, no
collection follows it, and no coverage or metric counts the successor's traffic
towards the retired event. It exists so the catalog answers the question a
sunset date raises and does not answer.

It is offered only when editing an existing event — a brand-new event has no
predecessor to name — and it is cleared, along with the sunset date, if the event
later leaves `deprecated`: a successor left on a live event would document a
retirement that was called off. The change is recorded in the event's own history
like any other tracked field.

On a **plan branch** the pointer is branch-local, like every other id in a
branch copy. Opening a branch copies the successor relationship onto the
branch's own events, so a branch's retired event points at the branch's copy of
its replacement, never back at `main`. Merging translates it the other way, onto
`main`'s rows, by event type and name — so naming a successor *and* creating it
on the same branch works, and `main` ends up pointing at its own copy. If the
branch's successor cannot be placed on `main` (it was deleted, or the branch
change was rejected), the pointer is cleared rather than left dangling. Deleting
the successor never deletes its predecessor; it only clears the pointer.

#### Names a scan writes for you

When a scan names the events of this type — its **Event name format**, e.g.
`{category}:{action}:{label}` — the form fills **Name** in from that template as
you type the field values, and the box becomes read-only. This is not a
convenience: the formatted name is also the event's *scan identity*, the key
collection matches on, so an event authored under a different name would never
merge with the traffic it describes. The rows the name is built from are marked
**names the event** and are required, and the form lists any that are still
empty. A placeholder that reads into a JSON field value, such as
`{payload.is_premium}`, is rendered the way a scan renders it: `true`, `false`
and `null`, and nested objects as JSON.

The rule follows the type onto a plan branch. A branch carries its own copy of
every event type while the scan names the `main` copy, so a branch copy
is resolved to its `main` counterpart by name and governed by the same format —
the form and **Add many events…** behave on a branch exactly as they do on
`main`. Every event type response (`GET /event-types`,
`GET /event-types/{id}`) carries the resolved rule as `event_name_format`,
`null` when no scan names the type.

An identity belongs to one event. If another event already answers to the name
being composed, the form links to it and refuses to create a second — a second
event with the same identity would receive no volume, no last-seen time and no
observed values, because collection only ever updates one of them. The API
refuses it too, with `409` naming the event that holds the identity, whether the
request comes from the app, the CLI, the MCP server or
`POST /projects/{slug}/events/bulk` (which applies the same naming rule per
item, rejects a batch whose own items collide, and prefixes its message with
`Event N of M: `). The rule is also a unique key in the database — one event per
scan identity per event type, on each branch — so two creates racing for one
identity end the same way: the loser gets that `409`, never a second event. A
scan that loses the same race adopts the event that won and carries on.

Renaming an event afterwards is safe and deliberately does *not* move the
identity — collection keeps matching the event it already knew. Once the two
differ, the event's **Properties** card shows the **Scan identity** row, and
`source_name` carries it in every event response. The human-readable label
belongs in **Title**, which is free to change and never touches the identity.
The same card tells **Created** — when the event was authored — apart from
**First seen**, the first metric bucket that counted it (`first_seen_at` in the
single-event response). An event planned before it shipped shows a dash there
until a collection sees it; a branch copy reads it through its `main` twin.

#### Adding many at once

**More › Add many events…** takes a whole run of events from a pasted block.
Opened from a type's tab, the page starts on that type. What the block carries
follows from the event type. Where a scan names its
events, each line carries the columns the name is built from — separated by a
tab or a comma, or the whole line where the format needs only one column, so a
path with commas in it survives. Where no rule governs the type, each line is
one event name.

Below the box, every line is listed with the event it would create and whether
it can be: `will be created`, `missing <column>`, `repeated above`, or `already
in the catalog`. Only the first kind is sent. Each name is looked up in the
catalog once the paste settles; until every lookup has answered, lines read
`checking…` and **Create** waits. A name whose lookup failed, or matched more
existing events than one lookup reads, or lies past the first 100 names, reads
`will be created, not checked`, and the list says how many there are — the
server refuses a taken identity regardless, so an unchecked line costs a
rejected submit rather than a duplicate.

A type with a required field that the name is not built from takes that field
as an extra column, after the identity columns and before the title: on a type
named by `screen_name` with a required `platform`, a line reads
`checkout_screen<TAB>ios<TAB>Checkout screen`, and the hint under the box gives
the column order. A single identity column — a free name, or a format with one
column — is split on a TAB only, so a name with commas in it stays whole; past
one identity column a comma separates too. An enum value outside the field's
options is caught on its line, e.g. `platform must be one of web, ios`.

Only a type with a required JSON field, or one whose name format reads a value
*inside* a JSON field, cannot be filled this way, and it says so instead. Add
those events one at a time.

Behind it is `POST /projects/{slug}/events/bulk`, which applies the naming rule
per item exactly as the single create does. It is one transaction: if any item
is refused — a missing required field, a taken identity, or two items claiming
one identity — nothing is created, and the message names the item by its
position in the batch.

Setting an event to `archived` takes it out of circulation on both sides:
`GET /projects/{slug}/events` leaves it out unless the request asks for that
status explicitly (`?status=archived`), so the CLI, the MCP `list_events` tool and any direct API
call agree with the app rather than each hiding it their own way; and metrics
collection skips it, so no new volume is recorded and its `last_seen_at` stops
moving. Archiving is reversible — set another status and collection resumes.

When a scan targeting the selected event type defines an **Event name format**,
new manual events use that same template. The form renders a live name preview
from field values, locks the name input, and blocks save until every referenced
scalar or JSON-path field is present. The backend treats the generated name as the identity and
returns advisory `warnings` if a client supplied a different name. Values saved
manually are marked as authored, so later scans add missing values but do not
overwrite the authored ones. Re-saving an event whose values did not change
keeps each value's authored flag as it was, and the branch diff does not report
a flag-only flip as a change.

### Event field, meta values & tags

Fields are defined on an event type (display name, name, type, required, enum
options, order); each event carries a value per field. Meta fields are
project-wide; each event carries a meta value per meta field. Tags are free-form
labels (lower-cased, trimmed, de-duplicated, and at most 100 characters each).
That normalisation happens on every door now — the form, the API, MCP and the
bulk paste — where once it happened only in the web form, so a tag written as
`Checkout` through an API client used to sit beside `checkout` as a second
label. Tags already stored keep the spelling they were given; nothing rewrites
them. The tag filter and the tag list case-fold, so `Checkout` answers to
`checkout` and the two show as one entry whichever way they were written.
Field and meta values accept variable
references (`${variable}`), and `url`/`date`/`json` field types render
type-appropriate inputs. A **meta** value is capped at 2,000 bytes once stored —
for a field with a link template, that is only the part the template wraps, not
the whole address you paste. The value itself is part of the uniqueness key that
stops one event carrying the same meta value twice, and a database index entry
has a size limit. A field value has no such key and is capped far higher, at
100,000 characters.

**Who owns a field value.** A scan fills field values in from what it observes
and keeps them up to date. The moment you type over one, it is yours: scans stop
touching that field, permanently, and the form says so under the box. To hand it
back, use **Hand back to scans** — that empties the box, and the next scan fills
it in again from live data. Clearing a field and correcting it therefore do
opposite things, which is worth knowing when a value looks wrong: correcting it
freezes your answer, clearing it asks for a fresh one.

**Seeing what else a field holds.** An event carries one value per field, so a
scanned value is one out of however many the event actually fires with. Under
each field, **Split volume by this field** jumps to that column's toggle in the
**Metric breakdowns** row and focuses it, so the column is switched on where the
event's breakdowns are chosen. Once the column is on, the line reads **Split by
this field** with a check and points back to that toggle; once collection has
data for it, **See every value this field takes** opens the event's
**Breakdowns** tab on that column, with a series and a count per value.

### Event discussion

**Where:** under the form on an event's edit page (Plan › Events → open an
event → **Edit**), and on the **New event** form as a single box above the
**Create event** button. A comment
needs an event to hang on, so the thread itself starts once the event exists —
but the question does not wait for it. A note written while authoring is posted
as the first comment the moment **Create event** succeeds. If that post fails
the event is still created and you land on it with your words in its composer
and the reason on screen, rather than losing them.

A threaded **Discussion** (top-level comments plus replies) for the questions
an event raises: "should this fire on cancel too?", "waiting on design". It is
deliberately **not** part of the plan. Title and Description describe the event
and travel with it — into search, the catalog, the branch diff, the approval
hash, and the Markdown spec pasted into the implementation ticket — so a
question written there arrives as if it were part of the specification. Nothing
written in the discussion goes to any of those places.

Every comment shows who wrote it and when, and replies nest under the comment
they answer. Only the comment's author or an owner can delete a comment on an event
or an attachment; the server refuses anyone else with a 403, and the **Delete**
button is shown only to them. It asks for a
confirmation that says how many replies go with it. The same thread powers the notes on an individual attachment and
the review comments on a branch, so all three read and behave alike.

An event has **one** discussion. Open the event on a branch and you see and add
to the same thread as on main, so a question raised while drafting a change is
answerable by whoever is reading the live plan, and merging the branch neither
duplicates nor loses it.

An event created on a branch has no row on main yet, so its discussion,
including the note typed when it was created, starts on the branch copy. If
main gets that event before the merge (a scan finds it, or someone adds it on
main, under the same event type and scan identity), the branch copy shows its
own thread beside main's and starts new ones on main's. The merge moves the
branch copy's thread to main: onto that same main event when main still has it,
so the discussion is not split between two main events when main holds the
event under a different display name. Otherwise it goes to the event the merge
creates or matches by type and name, but only when exactly one event has that
type and name on main after the merge and exactly one on the branch. In every
other case the thread stays with the branch: main has deleted the event, or its
event type, since the branch was cut, or several events share the type and
name, so the merge cannot tell which of them the thread is about.

#### Closing a question

Each top-level comment carries a resolution state, so a thread can end:
**resolve** it when it is answered, **snooze** it for a week when the answer is
not due yet, or **reopen** one that was closed too early. A snooze that has
lapsed counts as open again — the state is worked out when the thread is read,
not written back by a background job, so it is never briefly wrong. The date you
snooze *to* has to be in the future; one that has already passed is refused
rather than stored.

Resolution belongs to the **thread**, not to individual replies: a three-reply
conversation is one question, and only the top-level comment carries the
control. Reopening clears the resolution note along with the state, because a
reopened question has no resolution any more.

The events list can then be filtered by **Questions** — *Open questions* or
*Nothing open* — beside the Status, Silent and Reviewed filters. It is a
server-side filter over the whole catalog, not a narrowing of the loaded page,
and it is branch-aware: because an event has one discussion living on its main
row, a branch listing answers about the same threads main does. On a branch,
*Open questions* matches the events whose own thread or main twin has an
unanswered question; another branch's question on a same-named event does not
count. A row with an unanswered thread carries a small `?n` marker beside its
name, counted over the same threads, so the list can say why it matched. The
exception is several events on main sharing one type and scan identity (two
hand-written `purchase` events under `track`, which nothing forbids): a branch
copy of either matches through a question on any of them, while its marker and
its discussion come from only one of them, so it can match with no marker and
open on a thread that does not hold the question.

The **photo** threads and the **branch review** threads have no resolution
state; only the event discussion does.

### Event photos & specs

**Where:** the **Photos & specs** panel on an event's **monitoring detail** page
(Plan › Events → open an event, or click an event's signal on Observe ›
Anomalies). It is shown for the `event` scope only.

You can upload images (JPEG, PNG, GIF, or WebP by default, up to 10 MB each by default.
Both are instance settings, and the upload area shows the size limit your instance uses) by
drag-and-drop or the **Upload image** button (stored on the configured backend —
local disk or GCS). Several files upload side by side, each with its own
progress; a file that fails is named with the reason while the others still
land, and a file that is not an image is listed as not uploaded rather than dropped
silently. So is a file larger than the instance's size limit, which the page reads
from the server (`GET /api/v1/settings/photo-limits`, open to every signed-in user).
If the limit cannot be read, the file uploads and the server decides. An image type the
instance does not allow comes back as a failed upload with the server's reason. The drop zone
outlines only while a file is dragged over it, and an empty panel is a single line. You can also attach a
**Figma spec** from the **Attach Figma link** button, by URL with an optional title (rendered as an embedded frame with
an "Open in Figma" link), delete a photo or detach a spec, and hold a **threaded
comment** discussion (top-level comments plus one level of replies) per
attachment. On an event of a merged branch, or of a closed one until it is
reopened, the attachments are read-only like the rest of that branch's plan:
uploading, attaching, reordering or deleting one answers `409`. Their comment
threads stay open.

### Event types

**Where:** Plan › Event types, then open a type.

A type's page has three tabs — **Summary**, **Events** and **Settings** — and no
separate Settings button in its header. A new type opens on its **Settings**
tab, and its display name defaults to the title-cased name (`page_view` →
*Page View*). The Settings tab stacks: **General** (name, display name, color),
a **Fields** editor (add/edit/reorder/delete fields — type, required flag, enum
options, validation, and a level in the **Sensitivity** column), a **Sensitive
fields** summary, and **Owners** (shown only on the `main` branch — owners are a
fact about `main`'s type, so in branch context neither the list's **Merge
approval** column nor the detail's merge chip is shown, and nothing is asked
for). The list column reads **Owner approval** for a gated type and **Open**
for one without owners; the detail chip reads **Owner approval** or **Open to
merge**.

Validation offers what the field's type can use: **Regex** only for `string`
and `url` fields, **Min** / **Max** only for `number`. An `enum` field lists its
options and notes that values outside them are invalid. The share of invalid
values a field tolerates before it counts as drifting is **Max invalid share**. Owners gate
branch merges: a type **with** owners is "gated" (the branch needs a fresh approval from
one of those owners before an authorized editor can merge a branch that adds,
removes, or edits the type itself — its display name, description, color, or
order; a branch that changes only the type's fields or events does not ask for
one); a type with no owners has no owner-approval gate.
The gate is a review convention for the branch workflow, not an access control:
any editor can add or remove a type's owners, and an editor can still change the
type directly on `main`, which asks for no approval (see
[Security](../run/security.md#roles-and-access-control-rbac) on shared-project editing).

**Deleting an event type is refused with a `409 Conflict` while a scan is bound
to it.** The binding is what tells the scan where to put the events it collects,
and the database clears it on delete rather than refusing — so without the
guard the scan kept running, kept listing, and quietly collected nothing. The
message names every scan involved. Point the scan at another event type, give
it an **Event type column** so it discovers its types from the data, or delete
the scan; then delete the type. A branch's copy of a type is bound by nothing
and deletes as before. Merging a branch that removed the type is refused the
same way, for the same reason.

### Schema drift

Drift is detected when incoming data diverges from an event type's declared
schema and is surfaced as the **schema-drift badge** beside the heading of the
catalog. Drift kinds are `new_field`, `missing_field`, `type_changed`,
`enum_violation`, `required_null_violation`, `regex_violation`, and
`range_violation`. Per drift you can **accept**, **snooze** (defaults to 7 days,
and the date you pick has to be in the future), mark **false positive**, or
**reopen**. Only a snooze takes a `snoozed_until`; sending one with any other
action is refused with `422` rather than silently ignored. A resolution note is
optional on every one of them, and an action
that carries no note **leaves the stored note alone**
— re-snoozing a drift does not erase the reason somebody recorded last week.
**Reopen** is the exception and clears the note: a reopened drift has no
resolution to annotate.

Accepting a `new_field` or `type_changed` drift uses the same complex-type
classification as detection: BigQuery `RECORD`/`STRUCT` becomes a JSON field,
while scalar types remain scalar. A snoozed drift becomes active again once its
deadline passes. **Reset drifts** also removes schema-drift rows left behind by
a deleted scan, including rows whose scan reference was set to null.

Accepting a `missing_field` drift **deletes the declared field** from the event
type. tripl refuses that with a `409 Conflict` when a scan on that event
type builds its event names from the column — the plan cannot name its events
without it, and deleting the field would fail every subsequent collection with
*"the event name format references unknown keys"*. The message names the
column, the scan and its format. Fix it by editing the scan's
[**Event name format**](#event-detail--editing) so it no longer references the column, then
accept the drift. A **grouped** scan counts too — one with no bound event type
*and* an **Event type column**, which discovers its event types from the data
and so can produce events for any event type in the project. A scan with
neither does not: it discovers nothing and names nothing, so its format governs
no event type at all.

A placeholder is matched on its **base column**. A format of `{event.category}`
reads the `category` key out of the JSON `event` column, and that lookup only
happens for columns the event type still declares — so the format depends on the
field definition for `event`, and a `missing_field` drift on `event` is refused
exactly as one on `action` is.

A row that simply does not carry the JSON key is a different matter, and is not
a failure. `GROUP BY ALL` over the JSON paths gives a row that omits the key a
group of its own, which is ordinary warehouse shape rather than drift, so the
key contributes an **empty segment** exactly as a NULL regular column does. The
failure is reserved for a missing **base column** — one the query or the event
type no longer supplies — which is what the 409 above protects.

**Why the refusal exists.** A `missing_field` drift for the column `action` was
accepted in good faith on production, on an event type whose scan named
its events `{action}`. The field went away, every collection after it failed
behind the generic *"Scan failed due to an internal error"*, and that scan
collected nothing for four days before anyone connected the two. The 409 is a
redirect rather than a wall: if the column really has vanished upstream, the scan
is already failing whether or not the field definition exists, because the query
cannot supply a value the format needs. Editing the event name format is the one
repair that works in both worlds, which is why it is the only one offered.

:::warning `force` exists, has no button, and is not a convenience
The drift action route
(`POST /api/v1/projects/{slug}/event-types/drifts/{drift_id}/actions`) accepts
`"force": true`, which skips the check and deletes the field. It exists for one
honest case: a **project-wide** scan names the column in its format but
never actually produces events for this event type, so the guard fires on a scan
that was never going to break. It is **API-only by design** — no button in the
app, no flag in the [CLI](../run/cli.md#tripl-drifts). A warning next to an
Accept button is a thing operators click past, and clicking past it in good faith
is precisely how the four-day outage happened; typing `force` into a request body
is not something anyone does by accident.

A `force` request **must** carry a `note` (a blank one is a `422`), and that note
is stored on the drift as its resolution note, so the record of who overrode the
guard and why survives in the audit trail. If you are not sure the scan is
harmless, fix the event name format instead — that costs one edit, and being
wrong here costs a silent collection outage.
:::

### Meta fields

**Where:** Plan › Meta fields (it used to be called *Schema & fields*).
Project-scoped attributes every event carries whatever its type — owner team,
Jira ticket, review date (name, type, enum options, optional link template).
Per-type fields live on each event type. Create, edit, delete.

A link template must contain `${value}` where the key goes (`{value}` is
accepted and saved as `${value}`); the create and edit dialogs show a live
preview of the resulting link and keep **Save** disabled until the template is
valid.

A meta field with a **link template** (`https://tracker.example.com/browse/${value}`)
stores the bare key. Paste a full link that matches the template and the form
strips the template's fixed text before saving — the server does the same to
whatever a client sends — so `https://tracker.example.com/browse/TRK-42` is
stored as `TRK-42` and rendered back as the link. The field's caption shows an
example of what to paste.

**Several values on one event.** Tick **Multiple values** on a `string`, `url`,
or `enum` meta field and the event form gives it a chip input instead of a
single box — one event picked up in two tickets holds both keys, and each is a
link of its own. `boolean` and `date` fields are not offered the option: a
second value there is a contradiction, not a list. Repeats of the same value
are dropped.

Turning the option back off leaves values already stored on their events; the
form then shows the first one, and the next save of that event keeps only it.

### Variables

**Where:** Plan › Variables. Typed, reusable `${name}` placeholders referenced
from event field and meta values. Each variable separates **documented values**
from scan-observed contexts, and can bind to one or more warehouse columns or
dotted JSON paths. A per-event override replaces the global documented list for
that event.

The table shows documented/observed samples, binding paths, the events a
variable was **Observed in**, and open value drift; type chips show the schema
key (`string`, `number_array`). Observed samples accumulate across runs — re-sampling merges
new values into the stored list, under a cap, instead of replacing it — so the
observed column is a history of what has been seen, not a mirror of the latest
scan window. It also distinguishes two silences: it reads **No
values stored** when the variable has contexts but none of them holds a value,
and shows a dash only when no context exists at all. Drift can be accepted
globally or for one event, snoozed, marked false-positive, or reopened; rows
that are not asking for attention sit in both panels behind a toggle named for
what it holds — **Show N resolved**, **Show N snoozed**, or **Show N snoozed or
resolved** — and a scan reopens an accepted row on its own once it observes a
value outside the accepted set. An action without a resolution note preserves
the existing note; send an explicit null to clear it, or reopen the drift.
The event detail repeats the
affected event's review panel.

Each variable has its own page, `/p/:slug/variables/:id`, opened from
the `${name}` link in its row. Its tabs are **Definition** (name, type,
description, documented values and bindings), **Drift**, **Overrides** (the
per-event lists) and **Observed** (the scan-observed contexts, with **Clear
observed values**), each addressable with `?tab=` (`drift`, `overrides`,
`observed`; Definition is the default). The row's quick-edit dialog still holds
the same sections for a small change.

Selection enables a bulk bar with **Set type…**, **Set description…** and **Add
values…** — each a popover holding its own field and its own apply button — and
delete. A bulk type change is chosen first and applied with **Set type**, after a
confirm that names how many selected variables have documented values the new
type would reject. Values are checked against the variable's type wherever they
are entered — documented values, per-event overrides and bulk-added values: a
Number takes numbers, a Boolean `true` or `false`, a Date `YYYY-MM-DD`, a Datetime
an ISO date-time and JSON valid JSON (array types check each value as one
element). Changing a variable's type in its editor lists the documented values
the new type would reject, and holds Save until they are removed. **Exclude from scans** keeps a restorable tombstone so a deliberately
removed scan-owned variable is not recreated. Search matches a variable's
display name and description **and** its scan source path and bindings, so a
variable whose display name was shortened from a dotted path is still findable
by the data path it binds to.

A catalog run can end by **retiring the scan-created variables nothing refers
to any more** — no `${token}` in any stored event field or meta value, no
observed context, no value drift, no per-event override — so a catalog stops
accumulating rows minted from a JSON column keyed by free text. A scan you start
by hand runs this sweep, but scalar-derived variables are deferred if any scan
config in the project lacks a declared lookback. A **scheduled monitoring
collection** does it on every run for a variable minted from a path inside a
JSON column — a key that stopped arriving is exactly what the pass is for, and
the key's return mints the variable again under a new id — but judges a variable
minted from a scalar column only when **every** scan in the project sets
**Limits → Lookback (hours)**: with the field blank a scheduled run reads the slice it is collecting, often
a single hour, and a scalar column that looks enumerable for one quiet hour is
rewritten as literals in every event at once, which is not evidence that its
variable is dead. A **metrics replay** never does it: it syncs no catalog, so it
has no current view of which paths your rows carry at all. See [Variables &
templates](./variables-and-templates.md#unreferenced-scan-created-variables-are-retired-automatically)
for what a too-narrow view actually costs.

The pass is deliberately narrow: a typed display name, an edited description, a
hand-added binding, documented values, an override, drift triage, or an
**Exclude from scans** tombstone each keep the row, and a variable the run has just created is always
still referenced by that run's own event values. When a run retires anything it
says so in its [details list](#scan-runs).

An **All / In use / Unused** control filters the table by that same rule.
**Unused** is answered by the server with the retirement predicate itself rather
than by an "observed in no events" shortcut, so the count sitting under the
select-all checkbox is exactly the set a run would take — never a superset that
quietly includes rows a live event value still names. API clients pass
`usage=all|used|unused` on `GET /api/v1/projects/{slug}/variables`; the default
is `all` and an unrecognised value is a `422`. See
[Variables & templates](./variables-and-templates.md).

### Event-type relations

**Where:** Plan › Relations. Declare connections between event types; create and
delete. Relations are resolved per the active branch. The create dialog groups
the two ends as **From** and **To** and previews the join it describes; the
table shows that join in one cell, with the relation type in words (*belongs
to*).

All four ids a relation names — two event types and two fields — must exist on
the branch it is created in, and a request naming one that does not is refused.
A relation that points outside its own branch is not a relation anybody can
read: the diff, the merge and a branch copy all identify a relation by the names
behind those ids, so one that cannot be resolved took down whichever of them
reached it first. If a project stored such a row before the refusal existed,
creating a branch reports it by id and asks you to delete it.

### Tracking-plan branches & merges

**Where:** the branch switcher (top of the project sidebar) and Plan › Plan
branches. `main` is the live plan; feature branches let you stage changes before
merging. Working surfaces are scoped to the active branch via a `?branch=`
context. Switching branch writes `?branch=` into the address (or removes it for
`main`), so a reload and a copied link keep the branch you picked. Going Back
into an earlier page that named a different branch switches to that branch; if
the page you are on has unsaved changes, you are asked first. When the branch
you are working in is merged, closed or deleted, the app switches back to `main`
and says so. A link that opens a merged or closed branch on purpose (a merged
branch's diff, **Switch to** on an event from that branch) shows it read-only
instead. Merging an owned event type re-checks ownership (see
[Event types](#event-types)).

The list is split into **Open** and **Closed** tabs, each showing its count, so
landed work stops burying branches still in flight. `main` is listed on Open but
not counted — it is the base you work from, notwithstanding that it is stored as
a merged branch. **Closed** holds both merged and closed branches, each row
chipped with which. Opening a link to a merged branch selects the Closed tab for
you. **New branch** checks the name as you type, can switch you onto the branch
as soon as it is created (on by default), and also opens from the branch
switcher's **New branch from main**. A branch name must start with a letter or
digit, use only letters, digits and `-` `_` `/` `.`, and be at most 64
characters; the API refuses any other name with a `422`.

A branch's detail shows its name as the page heading (the breadcrumb reads
**Plan › Plan branches › &lt;name&gt;**), a **Draft · In review · Approved ·
Merged** progress line with a sentence naming the next step, and **Work on this
branch**, which switches you onto it. **Merge** sits in the action row with the
other transitions; **Reopen** on an approved branch reads **Move back to
draft**, and **Submit for review** is disabled while the branch has no changes.
On a branch with no reviewer, **Submit for review** first asks **Who should
review this?**: **Add and submit** adds the picked reviewers and submits,
**Submit without a reviewer** submits as it is, and **Cancel** leaves the branch
a draft.
A viewer gets no **Edit** on change rows. Conflicts cover every entity type
(event types, fields, events, variables, meta fields, relations), grouped by
type and parent, offer **Take main** and **Keep this branch**, and list the
values in the order **Was → Main now → This branch**; a row where one side
deleted what the other edited says so in words instead. The note that main has
moved on since the branch was cut is neutral ("safe to merge") when main
changed other entities only, and amber — linking to the conflicts — when an
entity the branch changed also changed on main or the merge would refuse. It
carries **Update from main**, which opens a dialog listing what main brings per
entity type and each overlap to decide; **Update branch** stays disabled until
every overlap has a choice, sends the choices with the update in one call, and
refuses with "Main changed again" if main moved since the dialog loaded. When
the merge would refuse over main's changes, **Merge to main** reads **Update
from main first** and opens the same dialog. The branch list explains its dot
in a legend: "Main has newer changes since the branch was created". In the merge
confirmation the overlap note appears only when main changed fields the branch
also changed.
Once your own approval stands, **Approve** is no longer the primary button. A long text change is
shown as one paragraph with the edits marked, not as two full copies.

The selected branch is part of the route (`/p/:slug/branches/:branchId`),
so a review is linkable. Each diff row expands to its field-level changes;
collection-valued fields (an event's field values and meta values, its tags, a
variable's documented values and per-event overrides) are broken out member by
member rather than dumped whole. A row also links to the entity it describes —
the event, event type, or variable — opened in the branch, or on `main` when the
branch deleted it. Events and variables additionally carry **Edit** on the
collapsed row, without expanding it first: an event opens its editor on that
branch, and a variable opens its own page
(`/p/:slug/variables/:id`) — including a renamed row, whose Edit reaches the branch-side copy
rather than the base one it is drawn from. A merged or closed branch offers no
Edit, matching what its writes would be refused for: the API answers a plan
write sent with `?branch=` naming a merged branch, or a closed one until it is
reopened, with `409`. That refusal comes after the route's own permission check,
so a caller who could not make the write anyway (a viewer, a read-only API key)
gets the route's `403` instead. Reads, and a search reindex, still work on
either. Photo and Figma spec writes, which address the event by its id rather
than by `?branch=`, answer the same `409` on such a branch's event; comments,
on a photo or on the event, are discussion rather than plan content and still
work there. A write that arrives while the branch is being merged waits for the
merge to finish and is then refused with the same `409`; a merge that starts
while a write to its branch is in progress waits for that write, and so merges
exactly what was approved or refuses the now-stale approval. An edit to main
made during any merge likewise waits and applies on top of the merged plan, and
a merge that starts during an edit to main waits and then reports it as a
conflict where it clashes with the branch, instead of overwriting it. A comment
posted on a branch's event during its merge waits too, and joins its thread on
main. Catalog rows, diff rows and the command palette carry the
branch in the link (`?branch=`), and an entity page opened that way shows a
banner naming the branch it belongs to, so a link handed to a developer opens
the right copy. A diff row also carries **warnings** for an event authored on
the branch without a scan identity — e.g.
`No scan identity: the naming rule 'track:{name}' needs name.` — so a reviewer
sees it before the merge lands an event that would never match its traffic.
Rows may share a name — two events called `purchase:success` under `track`,
which nothing forbids, or two relations between the same two fields. Every
branch copy remembers the `main` row it was made from, so the diff, the conflict
check, the merge and a revert follow each copy to its own row: deleting one of
two such events on a branch deletes exactly that one from `main` when the
branch merges, an edit lands on the row it was made to, and a branch that
deletes both copies and authors one event in their place leaves `main` with
just that event. Rows created on the branch are matched to `main` by name, as
before. A branch opened before this was tracked may still hold namesakes it
cannot tell apart; its diff row for such a name carries a warning to rename one
of the events, or remove one of the relations, before changing either. A variable renamed on the branch onto the name of a variable the branch deleted merges as
that rename: the deleted variable goes, and the renamed one keeps its id, its scan identity
and its observed values. When the deleted variable has no scan identity, or `main` changed
it after the branch was cut, the merge cannot tell the rename from an edit of that variable
and answers `409`; rename one of them and merge again. A branch
copy of an event reads its metrics, **last seen** and discussion through the
`main` event it was copied from (for an event created on the branch, the `main`
event with the same type name and identity), so the branch shows what the live
plan collected rather than blanks. Removals that are the machine's doing — a scan-minted variable still
exactly as the scan wrote it being retired (no binding beyond the scan's own, no
documented values or per-event overrides, not renamed, not excluded from scans,
not the removed half of a rename, and not named by a `${token}` in any field or
meta value of an event on the branch), or a removal `main` has already made
since the branch was cut — carry a `housekeeping` reason in the diff response,
are left out of the added/removed/changed counts (`summary.housekeeping` counts them), are
folded into one line under the list, opened on request, and are not what the
merge confirmation warns about. A branch named after a tracker
ticket (`WND-4770`) links to it from the detail
header through the first meta field whose link template takes a key, and a new
event opened in that branch has that meta field pre-filled with the key.
Branch comments identify their author using the current project roster.

**Revert** on a diff row (or on a single field-change row) puts that change back
to the branch's base state: an addition is discarded, an edit is written back,
and a deletion is rebuilt with its child rows (values, tags, overrides) and, for
an event, its successor (`superseded_by`). It never touches `main`, needs the
branch to be open, and refuses instead of half-applying: an event's photos
(their files are not in the plan snapshot), a child whose parent event type is
still deleted, and a change it cannot pin on one row, because several events or
relations answer to its name, or several events to the successor it would
restore. When those rows are in the branch's base snapshot, which is what the
revert restores from and which never changes, renaming on the branch does not
help: undo that change by hand. When the duplicate is the branch's own (the
base held at most one such row), rename or remove it on the branch, then revert.
A successor that no longer exists on the branch is cleared instead.

The branch policy can require a minimum number of **distinct approvals** and can
forbid self-approval. Approval hashes include event values, tags, photos (the
attachments, not the comment threads under them), ownership/review state,
variable overrides, and metric breakdown settings, so any later merge-relevant
edit makes the approval stale. Discussion is not a plan change: commenting on a
photo, on an event or on the branch leaves every approval fresh. Neither is the
order the database returns a multi-value meta field's values in: they are
hashed in sorted order.
Three-way merge preserves one-sided main and branch edits; divergent edits to
the same state and parent deletion versus a new child fail with a conflict.
When the merge removes an uploaded screenshot from main, its stored file is
deleted after the merge commits, unless another attachment, on any branch,
still uses it; a storage failure there is logged and never fails the merge.

An owner may configure a separate **Implementation tracker** for the project.
The implementation tracker currently supports Jira only. The API rejects other
`tracker_type` values instead of accepting a setting that the ticket worker cannot use.
When enabled, a successful merge best-effort creates one Jira implementation
ticket for the added/changed events; a scheduled sync promotes covered events to
`implemented` when Jira reports the ticket done. If Jira returns a temporary
transport or server error while creating the ticket, the worker retries up to
five times with backoff; each retry first searches for the branch marker to
adopt an issue already created by an earlier attempt. Collection completes the
lifecycle on its own: the first data an event receives promotes it from
`ready_for_dev` or `implemented` to `live`, while a `draft` or `in_review` event
stays where it is. This is branch workflow
automation, distinct from the Jira **alert destination** that creates incident
tickets from monitoring signals.

The merged branch's detail then carries an **Implementation ticket** panel: the
ticket key links straight to the issue in the tracker, next to the ticket
summary and a status chip that reads `Open` until the sync sees the issue done.
The panel is hidden — not shown empty — on branches that opened no ticket, which
is every branch until it merges with the tracker enabled. API clients read the
same list from
`GET /api/v1/projects/{slug}/branches/{branch_id}/implementation-tickets`; it is
read-only (tickets are written by the merge and sync workers) and open to any
authenticated user, and answers `404` for a branch that belongs to another
project.

An **event** sees the same rows from its own side. One ticket covers one
branch, and it records which events that branch touched, so an event carried by
three merged branches is named by three tickets — its detail page lists all of
them under **Implementation tickets**, newest work included, with the same
links and status chips. It is history, not the editable field: the panel is
hidden when nothing has named the event, and it never replaces a ticket key you
type into a meta field yourself. A branch copy of an event shows its main
counterpart's history, since the ticket names the event rather than one copy of
it. The list is at
`GET /api/v1/projects/{slug}/events/{event_id}/implementation-tickets`.

### Plan rules

**Where:** Workspace settings › Project › **Plan rules** (in the full-takeover
Settings area, route `/settings/project/plan-rules`). Not built yet: the page is
a single **Coming later** card that lists the planned naming, governance and PII
rules. It has no controls, because no backend contract exists for any of them,
and its rail entry carries a **Soon** tag.
The working branch-review controls live under **Plan → Plan branches → Merge
policy** (`min_approvals`, `block_self_approval`). Scan **Event name format** is
the working naming rule for scan-targeted event types.

### Project general & danger zone

**Where:** Workspace settings › Project › **General**. The rail's Project group
also has **Tracking plan & alerting**, which leaves the settings area for the
project's event types, alerting and the rest of its plan pages; the settings
search (⌘K inside Settings) finds a section by what it holds, such as
"timezone" or "delete project". Edit the project name,
slug, description and timezone (picked from the IANA zones the browser knows;
alert delivery schedules are read in it); set the project-wide number of app releases retained as
explicit version series; rebuild its search index; or use owner-only destructive
resets. Version retention applies to event monitoring and standalone catalog
metrics alike. Renaming the slug refreshes search links for the project's plan
branches so palette and Ask AI results point at the new route. Deleting a project
or resetting its demo clears its slug-specific catalog and monitoring caches.
The page has two cards below the fields: **Maintenance** holds **Rebuild
index**, the resets and **Retire unused variables**, and **Danger zone** holds
only **Delete project**. A read-only user sees the values as text, with no
**Save** or **Rebuild**. Deleting — owner-only, from this danger zone or from a
project's menu on the workspace page — asks you to type the project's slug
before **Delete project** arms, and a refused delete is reported inside the
dialog.

The workspace page lists projects as compact rows, each with a **Details** fold;
**Open incidents** is the figure that used to read *Alerts*, and a project with
nothing set up yet offers **Continue setup**. A project's latest-run line says
**N warehouse rows read** for a metrics run and **N combos grouped in the
warehouse** for a catalog run. When you create a project, its URL
slug is derived from the name and sits under **Customize URL**. If the server
rejects the slug as taken, the **Project URL** field opens with "Another project
already uses this URL. Choose a different one." under it.
Older releases are combined into **Other**. **Reset anomalies**
removes metric and breakdown anomaly records (and
their derived active signals), plus the stored expected-value bands charts draw,
across every scan/catalog metric. **Reset drifts**
removes schema and distribution drift, but not variable-value drift. Both can be
limited to a selected historical period and cannot be undone.

**Retire unused variables** applies the same retirement rule a catalog run
applies (see [Variables](#variables)) across a whole plan branch in one pass —
for the backlog that accumulated before runs started sweeping, and for the
scalar-column variables of a **Catalog + monitoring** config that sets no
**Limits → Lookback (hours)**, which its scheduled runs never judge. It is **two
buttons, not one**: **Preview** commits nothing and reports what the pass would
take, and **Retire** stays disabled until a preview says there is something to
take. Either way the row reports the same breakdown — how many rows can be or
were retired out of how many examined, and how many were kept because something
still references them, because they carry observed values, because they are
documented, because they were edited by hand, or because they are excluded from
scans.

The route behind it is
`POST /api/v1/projects/{slug}/danger/retire-unused-variables`, body
`{"mode": "delete" | "exclude", "dry_run": true}`. `dry_run` defaults to `true`,
so a call that omits it only reports; the response carries `scanned`,
`retirable`, `retired` and the `kept_*` counters the row renders.
`mode: "exclude"` tombstones instead of deleting — for a binding still live in
the warehouse that a later run would otherwise mint again — and is not offered
in the UI, which always deletes. The route honours `?branch=` and defaults to
`main`, records `project.retire_unused_variables` in the audit log when it is
not a dry run — and that entry names the branch it ran against, so a retirement
on a working branch is not read later as one on main — and takes the strict owner
gate: like the other owner-only administration routes it refuses an API key, so
it needs an owner signed in through the browser.

### Plan history & revisions

**Where:** Plan › **Plan history** in the sidebar (route
`/p/<slug>/history`). Named plan revisions (snapshots): create a
revision, list them, and diff any two. Each revision is labelled with its kind,
branch openings are folded together, and a diff is grouped by entity and can be
filtered. A merge or a branch opening links to its branch's review by id and
shows the branch's current name; a revision whose branch has since been deleted
shows the name it was created under, with no link. Distinct from per-event
history and the workspace audit log.

---

## Observe

### Overview

**Where:** Observe › Overview (the project's home page, route
`/p/<slug>/overview`; it used to be called *Live activity*). A status line under
the title sums up the project in one sentence — open signals, open incidents,
failing scans, broken alert channels (enabled destinations whose latest delivery
failed) and source trouble, each linking to where it is worked on. Below
it a KPI strip shows Active events, Implemented, **In review**, **Open signals**
and **Coverage** (on a working branch the plan counts are that branch's, so they
agree with its Events list); the last three are links to the review queue, the Anomalies
page and Coverage. Values show a placeholder while they load rather than a
"0". **Active signals** sits directly under the KPIs (the biggest few, with a
**View all** link to the Anomalies page carrying the full count). The **volume**
card, charted from a single scan and titled with that scan's name, leads with
the last 24 hours' total and its change against the 24 hours before, over a
dated axis with any anomaly marked, and an **Open chart** link to the full
monitoring detail. Then come a 14-day **new events** series (events added to the
plan per day on the main branch — not a history of the active-events stat),
**Top events · 48h** summed across every scan (each row opens its event and
shows its share of the project's volume in the same window; the shares need not
add up to 100%, since unmatched traffic counts in the total),
**Recent activity**, and **Source health**, whose rows carry a status chip and
link to the data source. A project that has never been scanned shows one
**Overview fills in after your first scan** state instead of empty panels, and
each panel loads with its own skeleton. While the activity rail is open beside the page (wide screens), the
page's own Recent activity panel is hidden rather than listing the same items
twice. Recent activity reads the **main branch** too, like the KPI series: an
open working branch holds its own copy of every event, and those copies are not
listed as separate entries. A row whose target has since been deleted is shown
without a link rather than linking to a page that no longer resolves. The volume card and the Events page's **Event volume** chart both
start from the same default scan — the most recently *created* one, so
editing an unrelated scan never re-points them. The Event volume chart departs from
it in exactly one case: when the tab's event type has no volume under that scan,
it charts the scan that *does* have volume for that tab rather than rendering an
empty card. A project whose event types are split across several scans — one per
event type is a common shape — would otherwise show nothing on every tab but the
default scan's own. Either way the chart names the scan it charted, so the two
surfaces never disagree silently. On a working branch the Event volume chart applies
the page's tag, status and search filters to the branch's own events — the ones
the table lists — and charts the volume their main-branch counterparts collected,
so a tag or status changed on the branch selects the same events in the chart as
in the table. A new project also shows a **Get started**
checklist (with a **What is this?** link to Concepts) that ticks steps off
automatically from real project state and hides itself once you are set up. Its
steps are **Connect a data source** → **Run a catalog + monitoring scan** →
**Review imported events** (with **Add events by hand** as the other way to
finish it) → **Define a key metric** (shown once the project reports a metric
count) → **Set up alerting**. A step's link opens its page with a **Back to
checklist** bar above it. The bare project address `/p/<slug>` opens this
Overview. It is role-aware: connecting a
data source is owner-only, so for an editor that step is shown as **Owner only**
with an ask-an-owner hint and is excluded from progress — a non-owner's checklist
can still reach done without it. Dismissing it offers **Undo**, and the command
palette's **Show getting started** row brings a dismissed checklist back.

### Top-bar alerts bell

The bell in the top bar is titled **Alerts**. Its badge counts open incidents,
the same number as the sidebar's Alerting badge. The popover lists **Open
incidents** first (each links to its Inbox card), then **Active signals**, then
**Recent alert deliveries**; retrying a Jira or Linear delivery from it asks
first. On **All projects** (the workspace pages) the badge counts every
project's open incidents, and the popover lists **Projects needing attention**,
worst first ("Checkout app · 1 open incident · 3 signals"): a project with an
open incident links to its Inbox, one with only signals to its Anomalies list.
Its footer link, **All projects**, opens the project list. See
[Alerting](./alerting.md). The destination dialog has no Channel
select on create — the channel comes from the **Add destination** item that
opened it — and edit shows the channel read-only.

### Alert rules

**Where:** Observe › Alerting › **Rules** (route
`/p/<slug>/alerting?section=monitors`; the tab used to be called
*Monitors*, and the `section=` key kept its old name so saved links still work). Every alert **rule** in the
project, across all destinations, in one list: the **condition** it watches for
(spike/drop direction, threshold, cooldown), the **destination** it routes to,
its **state** (firing / warning / healthy), when it **last fired**, and its
delivery health (`115 deliveries · 57 incidents · last 3h ago · sent`, or *Never
delivered*). A firing/warning/healthy rollup sits above the list. Each row
expands to the full labelled settings — scan binding, scopes, direction,
cooldown, thresholds, message template, filters. The enable switch stays on the
row; **replay**, **mute** (**1h / 24h / 7d**), edit and delete sit behind the
row's **…** menu, and opening **replay** runs it straight away. Saving, muting
and deleting confirm with a toast.
Open a rule for its detail page, which adds the fired history; its breadcrumb
reads **Observe › Alerting › Rules › &lt;rule&gt;** and the browser tab
**&lt;rule&gt; · Alert rule**.

The rule editor is four numbered steps — **What to watch**, **When**, **Where**,
and a collapsed **Customize message** — with an **Advanced** section after them.
A new rule alerts when the change is at least **30%**. The cooldown is entered
as an amount and a unit (minutes, hours, days) and saved in minutes. The guided
setup that creates a first rule includes a step for choosing the scan.

The rule editor and the monitor detail also mark an enabled drift scope whose
source data does not exist anywhere in the project — value drift with no
documented allowed-values list on main and no drift collected, distribution drift
with no scan watching a column and no significant drift collected — with an
inline notice linking to the screen that supplies it (**Variables**, **Scan
settings**). The toggle stays usable,
because the missing data can arrive later; the check is project-wide, so it says
nothing about the particular scan a rule is bound to. See
[When a scope is on but nothing feeds it](./alerting.md#when-a-scope-is-on-but-nothing-feeds-it).

A rule has no **indefinite** mute, and the asymmetry with the Inbox is
deliberate: muting a rule silences every scope it watches, so a rule you never
want to hear from again is a rule that should be **off**, and the enable switch
says that on the list where a permanent mute would read as *healthy, just quiet*.
The Inbox's per-incident mute does offer *indefinitely*, because it silences one
scope of one rule — see
[Silencing an incident](./alerting.md#silencing-an-incident).

This used to be a **separate nav item** with its own page, rendering the same
`AlertRule` rows under a second noun: a rule was read there and edited under
Alerting, which is how the two screens came to disagree about its mute state.
"Monitor" and "rule" are the same object, and it now has one home. `/p/<slug>/monitors`
redirects here.

Distinct from the **Monitoring detail** below: the per-scope volume drilldown
(chart, forecast, heatmap) is reached from an event or a signal, not from a rule
row.

### Monitoring detail

**Where:** reached from an event, an event signal, or a catalog row. Renders
per-scope metrics for an `event`, `event_type`, or `project_total` scope, with
tabs: **Volume** (series plus the latest signal as a strip — Flagged bucket /
Change / Actual / Expected / Deviation — and the forecast as a hollow point with
a likely-range whisker), **By version** with version-adoption (only when the scan defines an
app-version column), **Heatmap** (7×24 seasonality), **Distribution** (drift
bands), and **Breakdowns**. The page also surfaces top movers and release
regressions, plus chart annotations on the Volume tab.
On every volume and breakdown chart an anomaly is a triangle pointing the way
it moved — up and red for a spike, down and amber for a drop (an outlined bar in
bar style) — and its tooltip says so with the z-score. The time axis spans the
whole range you picked, so a series that began partway through starts partway
along rather than at the left edge, and a count's confidence band stops at zero.
The Volume chart draws the expected value and confidence band on every bucket
the detector scored, flagged or not; buckets scored before baselines were
stored (only each scan's evaluation window and replayed ranges gain one later),
buckets not scored at all, and the buckets folded into a reported level shift or
outage show no band (see
[Anomaly detection](anomaly-detection.md#the-baseline-band-on-the-chart)).
For an `event` scope it
additionally renders variable-value drift review and the Photos & specs panel.
An event that is not yet `live` also gets a **Spec** card ahead of the charts:
the scan identity with a copy button, the fields with their required and
**names the event** marks, documented variable values, an example payload, and
**Copy as JSON** / **Copy as Markdown** for pasting into a ticket. Optional
fields with no value are folded away on the card and left out of both copies.
The event's **Discussion** thread is on this page too.
For ratios, averages, and other non-count catalog metrics, the version legend
shows each version's latest observed value rather than a sum of daily values.
The range picker defaults to 7 days (30 for a catalog metric); when the scope's
open signal is older than the range you selected, the range is extended back to
that signal's bucket so the page shows what the list linked you to. That covers
both ways it happens — a long-running outage on an `event`, `event_type`, or
`project_total` scope, which is announced once at onset and then never
re-emitted, and a catalog metric flagged further back than the range under a
raised `recent_signal_window_hours`. Only a signal that is still **open** widens
the range; one that has since closed leaves your selection exactly where you put
it.

### Metrics catalog

**Where:** Observe › Metrics (route `/p/<slug>/metrics`). A project-wide catalog of
user-defined **metrics** — numbers tracked over time, the counterpart to the event
catalog. Each row shows the metric's name, kind, status (`draft` / `active` /
`archived`), interval, and latest signal state. Metrics are **not branched**: the
catalog reads the same on every branch.

The catalog supports kind/status/review-status/search filters, anomaly and stale-data filters,
reordering, uniform bulk status changes, duplicate-as-draft, manual **Collect
now**, archive/restore, and delete. Collecting a fact metric refreshes every
active metric that depends on the same fact table through the shared batch path:
compatible aggregates are folded into one warehouse query instead of rerunning
the fact-table SQL once per metric. A viewer who opens a metric's edit address
lands on the metric's page instead of a disabled form. Each catalog row shows
the metric's owner as an avatar when it has one. The create/edit form
picks a **kind** and then reveals kind-specific config; a new metric starts on
**From tracked events**, and a kind card says when the project has no events or
no fact tables to build from, and is faded (still selectable) until it has. A
project that tracks no events starts a new metric on **From a fact table**
instead, or on **Custom SQL** when it has no fact tables either. **Description**,
**colour** and the **internal name** sit under **More options**. Creating a new metric opens a template gallery
first: **Start from scratch** skips it, and a **Browse templates** link on the
form brings it back. The top bar names the edited metric: **Metrics › Active
Sessions › Edit** (a fact table's editor reads **Metrics › Fact tables ›
<table> › Edit**).

- **Custom SQL** — a data source, a read-only `SELECT` or top-level `WITH ... SELECT`
  returning one value per bucket, a time column, and a collection interval.
- **From a fact table** — a reusable fact table built from a read-only `SELECT` or
  top-level `WITH ... SELECT`, an **aggregation** (`count`, `sum`, `avg`, `min`,
  `max`, `count_distinct`), the **measure column** it runs over (a **distinct
  column** for `count_distinct`), optional row filters, optional **breakdowns**,
  and an interval. Row filters can mix reusable named filters from the fact
  table, structured column/operator/value conditions, and raw SQL fragments; all
  rows are combined with `AND`. Ratio fact metrics can also use breakdowns
  when both operands use the same fact table; each breakdown row is that value's
  numerator divided by that value's denominator, so breakdown ratios do not add
  up to the top-line ratio.
- **From tracked events** — derived from already-collected event series, set
  up under **Calculate**, with no
  warehouse query of its own: a **single** event's count, a **ratio** of one event
  to another (A / B), or an event **per distinct user**. Each side names one
  event — searched across the whole catalog — or a whole **event type**, which
  counts every event of that type.

Fact and event-composition metrics have a **Preview** card, a dry run of the
definition as it stands, over the last 50 buckets; nothing is saved. A fact
metric's preview queries the warehouse; an event metric's composes the counts
already collected, on the newest scan grid those events share. After the first
**Preview** it re-runs by itself whenever the definition changes. Behind it is
`POST /projects/{slug}/metrics/series-preview` (editor or owner), whose body is
the same `fact` or `event_composition` definition a save would send.

Every row of a fact metric's filters has to be complete before it saves: a named
filter with no name picked, a condition with no column or value, or an empty SQL
fragment is flagged in place rather than dropped. `in` / `not in` conditions take
one value per chip, so a value may itself contain a comma, and the operators on
offer follow the column's type. Saved filters reload grouped by type — named
filters, then conditions, then SQL fragments. Saved SQL the form did not join
itself (a lowercase `and`, a line break, extra parentheses) reloads as one row,
exactly as stored, and an untouched condition is saved back exactly as stored.

**Changing what a metric measures deletes its history.** Any edit to the
definition — the SQL, the data source, the interval, the fact table,
aggregation, columns or filters, the events, the composition or the kind —
deletes the metric's collected values, breakdowns and anomalies when saved, and
collection starts over. The edit form compares what it would save with what is
stored, says so as soon as they differ and asks before saving. That includes a
metric saved in a shape the form cannot send back unchanged — for example a
condition operator it does not know — where the warning shows before anything
was edited. Edits to the name, description, unit, color, status or dimension
columns keep the history.

A new metric is saved with **Create and start collecting**, or **Save as
draft** when it is not ready; a draft is not collected. A new **Custom SQL**
metric whose current query has not previewed cleanly — never previewed, edited
since, or the preview failed — asks before it is created, since collection
would most likely fail the same way. **Breakdowns** and
dimension columns sit in collapsed sections of the form. A metric carries a
reviewed mark, set with **Mark reviewed** on its page or in the catalog.

Shared fields are name, display name, description, color, unit, owner/review,
status, breakdown columns/limit, optional version/platform columns, and the
anomaly-detection toggle. With a breakdown limit, the values that stay explicit
are ranked once over each collection's whole window, so a long replay split into
chunks keeps the same values in every chunk instead of demoting a value into
**Other** part-way through the series. A metric is monitored only while it is **active** and
its anomaly-detection toggle is on. Turning that toggle off — or moving the
metric out of `active` — stops it being scored and closes its signal on every
surface at once: the catalog row, the metric's own detail page, the Anomalies
page and the sidebar badge, and it also stops being a candidate for alert rules,
so any alert already open on it closes on the next check. Anomalies already
recorded stay on the chart as history rather than being deleted. A metric is
collected only while **active**; `draft` metrics are saved but not collected,
and `archived` metrics stop collecting.

### Fact tables

**Where:** Observe › Metrics › **Fact tables**. A fact table is a reusable,
project-wide data definition behind fact metrics: a safe read-only `SELECT` or
top-level `WITH ... SELECT`, a timestamp column, previewed columns/types,
identifier columns, and named row-filter fragments, each written in a SQL
editor that completes the table's columns. The timestamp column is
picked in the **Columns** card and detected automatically from the previewed
column types. Preview runs the query with
a bounded sample so the form can validate and persist the available columns.

Fact metrics can reference the table's named filters, add structured
column/operator/value conditions, and add a guarded raw filter fragment; all
effective filters are combined with `AND`. Structured values retain their
column type, so numeric conditions compile as numbers rather than quoted strings.
A ratio can combine two fact
operands, including operands from different fact tables. Fact tables and metrics
are indexed by global search and are not copied into plan branches. A viewer
opening a fact table sees its definition read-only (query, timestamp column,
columns and filters as labelled values) rather than the editor.

The list shows each table's data source, timestamp column, and **Used by** —
how many metrics read it. That count includes every metric that reads the table,
so a ratio metric that uses it only as the denominator counts too. The stat
strip's **Tables in use** counts the tables at least one metric reads, and
**Used by** links to the metrics catalog narrowed to the metrics that read that
table (`?fact_table=<id>`; API clients pass `fact_table_id` on
`GET /projects/{slug}/metrics`). A row's **⋯** menu has **Edit**, **Duplicate**
and **Delete**. In the editor, after the first **Preview** the column preview
re-runs by itself as the SQL changes, and a new table takes the next palette
colour no other table uses.

A fact table that metrics still read cannot be pulled out from under them.
**Deleting** it, **unbinding its data source**, **removing or renaming a named
row filter a metric uses**, and **removing or renaming an introspected column a
metric aggregates, breaks down by, or filters on** are each refused with a
conflict naming the metrics in the way (up to ten, then a count of the rest).
Every metric that references the table counts, whatever its status — a `draft`
or `archived` metric hits the same dangling reference the moment it collects
again — and so does a ratio operand, whose reference lives inside another
metric's config rather than in a column of its own. Change or delete those
metrics first.

The column refusal is the one you are most likely to meet by accident: it fires
on the ordinary re-preview-and-save flow, when you edit the SQL so it stops
projecting a column and save the freshly previewed column list over the old one.
A single save that prunes several used filters — or several used columns — is
refused once, naming every blocked name, so there is no need to iterate one
round trip per name. Refusals across categories are still reported one category
at a time: filters first, then columns, then the unbind.

Adding a column or a filter is unaffected. Dropping one is not, and neither is
the unbind case below — which matters if you drive the API directly. The unbind
refusal is decided by the `data_source_id` **in the request**, not by comparing
it with the stored one, so a client that resubmits the whole object with
`data_source_id: null` is refused even when the table already has no data source
bound. That is exactly the state a *deleted* data source leaves behind — the
fact table survives and its `data_source_id` is set to `NULL` — so the refusal
lands on the edits made to repair such a table, and names metrics that are
already failing for the unrelated reason that there is no source to read. Send a
real `data_source_id` in the same request to get the edit through. The editor on
this page never trips this: it refuses to save at all without a data source
selected.

**Which data sources a fact table may bind.** The same rule `sql`-kind metrics
use: a source is available to this project unless it is identifiably another
project's — that is, its owning project is a different one, or it is a shared
(unowned) source that some other project scans and this one does not. A shared
source **no** project scans is available everywhere. Anything else is refused
with `404 Data source is not available in this project.`, the same sentence on
the save and on the preview. See
[Security → Roles and access control](../run/security.md#roles-and-access-control-rbac).

### Metric detail

**Where:** open a metric from the catalog. The drilldown **reuses the monitoring
detail tabs** (Volume with the latest signal, Heatmap, Distribution, Breakdowns)
for the metric's own scope, so a metric reads like any other monitored series.
The header offers **Activate** on a draft, **Recompute** for a fact-table metric
(it refreshes every active metric on that fact table) and **Collect now** for
the other kinds, **Edit**, and a
**⋯** menu with **Create alert…** (the rule form, already scoped to this
metric) and **Delete metric…**. A metric with no description shows an **Add a
description…** link to the editor. With no values yet the chart says why: a
draft "is a draft and isn't collected"; a fact-table or SQL metric reads "No
values yet — compute now or wait for the next scheduled run".
Its definition card links fact-backed operands to their fact tables, shows the
next scheduled collection (or an explicit due state; *Not collected while
draft* for a draft, and a **Set a schedule** link to the editor for an active
metric with no interval), and exposes
the generated primary collection SQL in a collapsed, read-only editor. This is
the same time-windowed, multi-aggregate statement shape the collector executes
for all compatible dependent metrics on that fact table and interval; separate
breakdown scans are not included in this preview.
The schedule advances after a successful empty collection as well as one that
writes values, so an empty source window still has a concrete next update.
Count-shaped metrics (counts/sums) and fractional metrics (ratios, averages, SQL
values) are scored differently so a ratio that naturally sits below 1 isn't
constantly flagged — see [How anomaly detection works](./anomaly-detection.md).

### Anomaly signals

A signal is emitted when the latest bucket for a scope deviates from its
baseline. Signals appear on the overview, as catalog row badges, in the activity
feed, and on the monitoring detail. Tuning lives at the project **Detection
settings** (route `/p/<slug>/settings/monitoring`): toggle anomaly detection,
choose the scopes to watch (project total / event types / events / metrics), and set the
baseline window (buckets), minimum history (buckets), sigma threshold, minimum
expected count, the open signal window (hours, 1–720, default 24) and the
ingestion-settling allowance (minutes, 0–1440, default 120). Scans
honor these settings. The last two are refused when they collide — the settling
allowance must stay strictly below the open signal window, or every signal would
be stale before it could be scored and the Anomalies page would read zero while
alerts kept firing (see [Detection
latency](./anomaly-detection.md#detection-latency)). The same page lists **Scope overrides** — the scopes that
marking an alert a **false positive** has permanently tightened, each showing the
scan, the sigma threshold and minimum expected count now in force for that scope
alone, and how many false positives produced them. **Remove** puts a scope back
on the project settings; the project settings themselves are never changed by
that feedback. Detection settings only decide what gets **flagged** —
they never notify anyone by themselves. Notification delivery is a separate,
fully available layer: route the resulting signals to Slack, Telegram, a webhook,
email, Jira, or Linear under **Observe › Alerting** (see
[Alerting rules](./alerting.md)).

### Anomalies

**Where:** Observe › Anomalies (route `/p/<slug>/anomalies`). A standalone,
cross-event list of every open monitoring signal, biggest first by
**relative effect** (`relative_effect`, with `|z|` only breaking ties) — the same
order the Overview's Active signals panel and the top-bar bell use, so a quiet
scope with an inflated z-score does not lead the list. A rollup shows open-signal,
spike, and drop counts. The list has four columns: **Anomaly** (spike/drop
direction and scope — project total / event type / event / metric), **Change**
(the % change from expected with **Minor** / **Significant** / **Major** beside
it; the z-score is on hover), **Actual / expected** (in the metric's unit when
the signal carries one), and **When** — the absolute start of the bucket it
fired in, and under it when the detector found it ("found 16m ago"). On a daily
or weekly scan that start can be days before the detection itself. On a phone
each row folds onto two lines. Each row is a link to the monitoring detail for
that scope, so it can be opened in a new tab. The scan and magnitude filters
are dropdowns in the filter bar. When a series drops all the way to zero, the
Change column reads **dropped to zero** instead of a percentage. A project where
no monitoring scan has collected yet shows **Monitoring isn’t running yet**
rather than an empty list. A scope that
dropped to zero and has not emitted since stays on this list for as long as it is
down, rather than ageing out of the open-signal window after a day — the outage
is announced once, so tripl re-checks whether it is still down instead of judging
it by the age of that one announcement (see
[An event that goes silent is reported once](./anomaly-detection.md#an-event-that-goes-silent-is-reported-once)).
That covers a scope which had volume to lose. A scope whose expectation was
itself zero is not held open: nothing was lost, so it ages out normally, and the
open-signal rollup at the top of this page cannot accumulate zero-versus-zero
rows forever — and no new ones arrive, because the detector no longer reports a
bucket that had neither an expectation nor any traffic. Those rows sit below the
default **Significant** magnitude filter, so what this changes is the rollup's
count rather than the list under it.
When one incident trips several scopes on the same bucket,
the child rows (event type / event) are still shown and tagged `within total
spike` or `within total drop`, following the direction, rather than folded into
the project-total row. A **magnitude filter**
(All / **Significant (≥50%)** / **Major (≥100%)**, defaulting to **Significant**)
trims the list by relative effect (`|actual − expected| / max(expected, 1)`). A **scan filter** sits
beside it whenever signals come from more than one scan, with a count on each
option, so a large legacy scan cannot bury a smaller live one purely by watching
more events; catalog metrics are project-wide rather than scan-bound and get
their own option. Both filters narrow the list already in memory — no extra
request — and the counts on the scan options are taken from the whole stream, so
raising the magnitude cannot make the option you are standing on disappear.
**Both are in the page URL.** The magnitude filter is
`?level=<all|significant|major>`, and the default (**Significant**) writes no
parameter; a level the page does not recognise degrades to that default. The
scan filter is `?scan=<scan_config_id>`: it opens the page already
narrowed to that scan, and picking an option writes the parameter back (choosing
**All scans** removes it), so a narrowed view can be shared or bookmarked, and
opening a signal to investigate it and pressing Back returns the filters you
were using rather than resetting them. This
is where a scan run's **Signals added** counter links to. A scan with nothing
open right now keeps its selection and says *No open anomalies from &lt;scan&gt;* —
a signal closes once the metric comes back to normal, so an older run's link
lands here — with **Show all scans** one click away. Only an id this project does
not have — a deleted scan, a stale bookmark, a hand-edited URL — degrades to
**All scans** and shows the full list. Neither case ever swaps a different
scan's anomalies in for the one you asked for. The
sidebar and top-bar badge, the Overview **Open signals** stat, and this page all
report the **same** number — open signals across every scope that clear the
Significant threshold — so the badge agrees with the list rather than reading
lower. Sensitivity is tuned in **Detection settings** (see
[How anomaly detection works](./anomaly-detection.md)).

### Chart annotations

The **annotations** layer on the monitoring Volume tab lets you mark a deploy or
release at a bucket time with a label, optional description, and color; scoped to
the chart and deletable.

Every annotation carries a **source**, and two of the three are written by
something other than a person at the chart:

| Source | Who creates it | How it draws |
|--------|----------------|--------------|
| `manual` | Someone using the annotation form on a chart (or **Annotate** on a signal). | As before: the colour it was given. |
| `release` | The metrics worker, automatically, when a new app version goes live. | Muted, with a tag icon. |
| `api` | A deploy pipeline, through `POST /projects/{slug}/annotations` or [`tripl annotate`](../run/cli.md#tripl-annotate). | Muted, with a rocket icon. |

**Automatic release markers.** When a scan has an **app-version column**, the
metrics worker draws one project-level marker, labelled **Release *version***,
the first time a version becomes **active** — the same maturity gate
[release regression](./anomaly-detection.md#release-regression) uses: at least
5% of total traffic by default (the scan's own minimum share, if set) for two
consecutive buckets, with a minimum release volume behind it. The marker sits on
the bucket where the version became active, not on the first stray event from a
tester's device. Three consequences:

- A scan with no app-version column never creates one.
- The **first** version the scan sees gets no marker, and neither does one that
  was already live when the scan started looking: there is no earlier release
  for it to be a change from, and its activation bucket is not when it shipped.
  Markers start with the next release to activate after it.
- There is **one marker per release label per project**, ever. Re-running a scan
  or replaying a period does not draw it again, and prerelease builds excluded
  from the gate never get one.

**Pipeline markers** (`api`) carry an optional **link** — the release notes or
the pull request — and the tooltip opens it in a new tab. The same label posted
again within 24 hours returns the existing marker instead of drawing a second
one, so a retried deploy job is harmless. Only `api` and `release` markers are
de-duplicated (`release` permanently, one per label per project); `manual`
annotations never are — every one you add from the form is created, even with a
label you used a minute ago. See the
[Agent API guide](../integrate/agent-api-guide.md#chart-annotations) for the
request and a GitHub Actions example.

**Show releases.** A **Show releases** toggle appears under a chart only while
that chart currently shows at least one `release` or `api` marker. Turning it
off hides the `release` and `api` markers on every monitoring (Volume tab) chart
in the project and leaves
your `manual` annotations exactly where they were. It is remembered per user,
in this browser, and starts on.

### Alerting

**Where:** Observe › Alerting (Inbox, Rules, Destinations, Delivery log). Destination channels:
**Slack**, **Telegram**, **Webhook**, **Email**, **Jira**, **Linear**. Routing
rules carry a **cooldown** (minutes); an optional **Scan** binding
(`scan_config_id`, default **All scans**) that narrows a rule to one scan
configuration — metric-scope anomalies are project-wide and are never delivered
by a scan-bound rule, and deleting a scan unbinds and disables the rules bound to
it rather than widening or deleting them; filters on `event_type` / `event` /
`direction` with operators `=`, `!=`, `IN`, `NOT IN`; thresholds for minimum
percent delta, minimum absolute delta, and minimum expected count; an **include
variable value drift** opt-in alongside schema, distribution, and release drift;
and message and items templates with variables such as `${channel}`,
`${destination_name}`, `${rule_name}`, `${scan_name}`, `${scope_label}`,
`${matched_count}`, and `${items_text}`. Metric-scope anomalies are also safe-off
and are enabled by the rule editor's **Metrics** box (`include_metrics`). A rule can be
**simulated/replayed** over the last N days (default 7), optionally overriding
the saved cooldown, minimum percent delta and minimum expected count, plus the
detector's sigma threshold — a project **Detection settings** value rather than a
rule field, accepted only above `0` and up to `10`. Every override applies to
that one run and is written back nowhere. The **Inbox** is one row per incident — keyed by *(scan, rule,
scope, direction)*, or by *(rule, scope, direction)* for a catalog metric, which
is measured once for the whole project, names no scan, and so gets one shared
incident whose acknowledgement holds for every scan — with six actions:
**acknowledge**, **resolve**, **mute**
(**1h / 24h / 7d / indefinitely**), **reopen**, **false positive**, and a
standalone **note**. Acknowledge, resolve, mute and false positive all stop
further deliveries for that incident; only a mute outlives the incident, and only
a false positive changes detection. See
[Silencing an incident](./alerting.md#silencing-an-incident). Incidents also
carry a **checkbox**, and a selection raises an **N selected** bar offering
**acknowledge**, **resolve**, **reopen**, **note** and **mute** (the same 1h /
24h / 7d / indefinitely) — the same levers applied N times, not new ones.
**False positive is not offered in bulk** and the API refuses it with a **422**:
direction is part of the incident key, so a scope's spike and its drop are two
rows, and marking both would ratchet that scope's thresholds twice for one
decision. A bulk note is **copied** into each incident rather than shared, a
bulk mute confirms first, the selection is capped at **200**, and the action
applies to the whole selection or to none of it.
`POST /api/v1/projects/{slug}/alert-inbox/bulk-actions` (editor role) takes
`correlation_group_ids` plus the single route's action fields and returns the
rebuilt cards, a **batch id** and `overrides_written` (always `null`); each
affected incident gets its own audit-log row under that shared batch id. See
[Acting on several incidents at once](./alerting.md#bulk-actions). The **Delivery
log** lists deliveries filterable by status (pending / sent / failed) with retry
on failures, plus channel, destination, rule, and **scan**. That third section's
`?section=` key is still `audit` — the label changed, the link did not, and it is
not the project-wide **Govern › Audit log** below.

The scan filter is deep-linkable the same way Anomalies' is:
`/p/<slug>/alerting?scan=<scan_config_id>` opens the delivery log already
narrowed to one scan, which is where a scan run's **Alerts queued** counter
links. As on Anomalies, an id this project does not have degrades to **All**
once the scan list resolves, so a link to a since-deleted scan shows the full
delivery log rather than a permanently empty one.

---

## Govern

### Reconciliation

**Where:** Govern › Reconciliation. **Data match** shows the share of planned
events actually seen in your data over a fixed 14-day window (named by the
"Last 14 days" label on the panel). The per-day bars sit on a fixed 0–100% scale;
a day with no data is drawn as an empty dashed outline rather than as 0%, and a
match that never changes is stated in words ("Stable at 94% in every hour with
data over the last 14 days") instead of as a flat line. A project where no scan
has run yet shows **Nothing to reconcile yet** with a **Go to Scans** button
instead of empty panels; once a scan has run, the panels (and the shadow inbox's
Accepted / Dismissed tabs) stay even when nothing was read in the window. In the shadow events inbox each row's
**Accept** is an outline button, and the bulk accept is the one primary action. Screen readers get a summary (lowest, highest,
latest, days without data) and a per-day table. The headline percentage carries an inline tooltip spelling out
that it measures data match — not the Coverage page's plan coverage — so the two
governance numbers are not read as contradictory. The **shadow events inbox** (tabs: `new` / `accepted` /
`dismissed`) lists events seen in data but missing from the plan — **Accept**
creates the event on the active branch (you pick an event type when none is
inferred), or **Dismiss** it. The inbox loads 100 rows at a time and says how
many it is showing ("Showing 100 of 812"); **Show more** loads the next 100, as
far as the list goes. The `new` tab carries the count of new events. Tick rows
(or the select-all box) to **Accept** or **Dismiss** them in bulk. A row's
**Show N samples** opens up to five rows the last collection saw for that
identity, each as its column/value pairs, so you can see what the event looks
like before accepting it (`sample_properties` on each candidate; empty until a
collection has observed it). A bulk action
is one request (`POST …/reconciliation/shadow-events/batch`), and each row
succeeds or fails on its own: a refused row shows the reason on the row and the
others still go through. Bulk accept only takes rows that already have an event
type, and rows without one are accepted individually. A scan reads `main`'s plan, so the event type it
inferred is `main`'s; accepting on a working branch writes the branch's own copy
of that type, matched by name. If the branch deleted the type, the accept is
refused and says so — accept on `main`, or pick a type the branch still has.
An accepted event carries the identity the scan observed and **no field
values**: nothing in the inbox could have supplied them. Its required fields
therefore open empty until somebody fills them in, which is the honest state —
the scan has seen the event, the plan has not been written yet. In a scan grouped by an event type column one
generated identity can turn up under more than one event type, and such an
identity is a single inbox row carrying the combined volume across those types,
attributed to the event type that contributed most of it. That happens because
the identity is built from the row's own column values and the event type column
is normally reserved out of that set, so two types collide whenever the values
the name *is* built from coincide. Naming the **Event type column** in the
**Event name format** is the one route that puts the type into the identity (see
[What the scan form asks](#what-the-scan-form-asks)), and it usually separates
them — but it is not a guarantee, because an event group rule that rewrites both
names to one folds them back together anyway.
**Dead events** (in plan, no data in the last
30 days) can be selected and archived; archiving asks for confirmation with the
count and reports how many events it archived. Dead events are computed on the
project's `main` branch and archiving writes to `main`, so on a working branch
the panel is labelled "main branch" and does not offer **Archive** — switch to
`main` to archive. Long lists show 200 rows at a time. Accepting or archiving
refreshes Coverage, the event lists and the data match straight away.

**Archiving puts an event away for good.** An archived event is inert: scans stop
refreshing its field values, group rules can neither rewrite nor delete it, and
its warehouse volume leaves data match on *both* sides — it counts as neither
matched nor unmatched. So archiving a busy event does not move the percentage,
and the archived identity never comes back through the shadow inbox as an
"unmapped event". If an archived event is still arriving, the scan run says so:
its **run summary** reports how many archived identities were seen and how many
rows they carried, deliberately kept off the data-match percentage rather than
folded into it. Its volume also still appears in the event type's volume series.

**One event for a family of legacy names.** A legacy event whose name varies in
its tail — `profile_click_boat`, `profile_click_kite`, `profile_click_fish` — is
one concept in the plan and many identities in the warehouse. Two things look
like they should join them up, and neither does:

- **Scan identity is derived, never authored.** The server stamps it from the
  governing scan rule. There is no box to type a second name into, so leaving one
  "empty" is not a choice you have.
- **A `${variable}` in a name-forming field is stored literally.** The event form
  validates the token and offers its documented values, but the naming rule
  substitutes `{column}` only — so `profile_click_${forecast_profile}` becomes an
  identity that matches nothing at all. The form now says so next to the field.

What does join them up is an **event group rule**, under
Settings › Scans › *Event names and grouping*:

| Field | Pattern | Group name |
| --- | --- | --- |
| `__event_name` | `^profile_click_` | `profile_click` |

The rule rewrites the derived name before the plan is matched, so every future
scan files the whole family under one event. It also works backwards: **Apply
event groups** on the scan folds catalog events that already exist into the
survivor, carrying over everything listed below.

**Apply event groups** acts on the **main plan only**. Events on an open working
branch are left exactly as their author left them: the branch's own copies are
not folded, and merging the branch does not apply the rule to what it brings in
either. Run **Apply event groups** again after the merge to fold those.

`__event_name` is always the derived identity. `event_name` normally means the
same thing — but when the warehouse itself has a column called `event_name`, a
rule on `event_name` matches that column's raw value, and the identity
pseudo-field fills in only where no such column exists. Rules read every column
of the row, not just the ones that became event fields: the reserved role
columns (event type, app version, platform) and the rule columns themselves are
all matchable, and none of them becomes an event field. The one column a rule
cannot match on is the **Time column**.

**What a group merge carries over.** When a group rule folds several events into
one, the survivor inherits the settings that pointed at the events it absorbed,
so a merge does not quietly undo work you did:

- **detector sensitivity.** A scope you tuned by marking false positives keeps
  its threshold. Where both events were tuned, the stricter setting on each knob
  wins and the false-positive counts add up — the ratchet only ever tightens, so
  a merge can never loosen it.
- **alert rule filters.** A rule that names the event goes on naming the
  survivor, in both directions: an *is* filter keeps covering that traffic and an
  *is not* filter keeps excluding it.
- **chart annotations.** An event-scoped marker moves to the survivor's chart.
  Its text is left exactly as written — only where it is drawn changes.
- **open implementation tickets.** The survivor is still flipped to *implemented*
  when the ticket closes. A closed ticket is left alone; it records what shipped.
- **metric operands.** An event-composition metric follows the survivor, and so
  does the volume, since the merge already sums the series onto it. The one
  exception is a **ratio** whose numerator and denominator both end up on the
  same event: that would compute a flat 1.0 forever, so the metric is marked
  failed instead, naming the events involved so it can be redefined.
- **lifecycle status.** The survivor keeps the furthest-along status of the
  events it absorbs, but only along the `draft` → `in_review` → `ready_for_dev`
  → `implemented` → `live` progression. Retirement is never inferred in either
  direction: folding a `deprecated` member into a live group does not retire the
  group, and folding a live member into a group you deprecated does not
  un-retire it. Where the merge has to create the group event itself, the new
  group event always starts at `in_review` — the same place every event a scan
  mints starts — whatever status the member it was minted from carried, and
  neither the sunset date nor **Replaced by** is carried across. Its members
  then fold in exactly as above, so anything further along still lifts it. What
  that fixed starting point settles is the two cases the fold cannot reach: a
  group minted from a `deprecated` or `archived` member is never born retired
  with no sunset date, and a group whose members are **all `draft`** arrives in
  the review queue rather than as another draft — folding `draft` into
  `in_review` leaves `in_review` standing, so the group is one you are asked to
  look at even though nothing it absorbed had been.

Variable data moves too — see
[Variables and templates](./variables-and-templates.md#when-a-scan-merges-events-into-a-group).

**What deleting an event clears.** A merge has a survivor to move things onto; a
delete does not, so the same references are removed instead. Deleting an event —
on its own, in bulk, or by deleting its event type — clears its stored
anomalies, its tuned detector sensitivity, and its event-scoped chart markers,
and drops it from any open implementation ticket.

Two consequences are worth knowing before you delete rather than archive:

- **an alert rule that named only that event is switched off.** Its filter row
  goes, and rather than leave the rule pointing at nothing — which would quietly
  widen it to everything its destination watches — the rule is disabled. It
  keeps its name, thresholds and templates, and the Alerting tab shows it off
  rather than silently re-aimed. A rule that *excluded* the event stays enabled:
  "exclude these three" genuinely becomes "exclude nothing" once they are gone.
- **the event's anomalies are deleted, not orphaned.** Until this changed they
  outlived the event and, having no event to be filtered on, matched every alert
  rule — so deleting an event could start alerts that archiving it would have
  stopped.

**Archiving remains the non-destructive option**: an archived event keeps all of
this and simply goes quiet.

### Coverage

**Where:** Govern › Coverage (route `/p/<slug>/coverage`). A read-only
plan-coverage overview, complementary to Reconciliation's data-match view. The
rollup leads with **plan coverage** — the canonical share of active events that
are implemented — alongside active, implemented, **In review**, and archived
counts, plus an implemented-vs-not-implemented bar. The bar's remainder is
labelled "not implemented" rather than "pending" because it is the arithmetic
remainder (active − implemented) and therefore includes draft and ready-for-dev
events, not just the ones awaiting review. The "N not implemented" figure links
to the Events list filtered to exactly those statuses, and a line under the bar
breaks it down ("Not implemented: N in review, M draft, ready for dev or
deprecated"). An inline tooltip on the
plan-coverage figure clarifies that it counts implemented events, not events
seen in warehouse data (Reconciliation's data match), so the two views are not
confused. **Instrumentation gaps** lists **implemented and live** events with no
data in the last 30 days (the same dead-events signal Reconciliation acts on),
excluding events created inside that window; each row shows the event, its type,
and when it was last seen, with a link to Reconciliation to triage. The panel
names that basis inline, because the Events page's "Silent &gt; 30d" filter spans
every non-archived status and therefore reports a larger total.

### Scans

**Where:** Govern › Scans (route `/p/<slug>/scans`; requires a data source). The
legacy `/p/<slug>/settings/scans` path still resolves — it redirects here, so old
bookmarks and links keep working.

**New scan** opens its own page at `/p/<slug>/scans/new` (owners only), so
**Back** returns to the list and a reload keeps you on the form; creating the
scan takes you to its page, where **Run now** is. A scan's page remembers its
tab in the address (`?tab=configuration`), so a reload stays on the tab you
were reading.

Every scan surface states the chain a scan feeds, because a scan's output reaches
you as anomalies and alerts and nothing on these screens used to say so. The list
says it once for all scans; the form says it under the mode you have selected;
and a scan's own page says what *that* scan does today — including the case where
it has a schedule but no time column, so the scheduler never runs it and it
collects no metrics. A run's
**Signals added** and **Alerts queued** counters are links back out to the
[Anomalies](#anomalies) and [Alerting](#alerting) surfaces filtered to that scan
(`?scan=<scan_config_id>` on both). The link filters by **scan**, not by run — a
run's stored summary carries counts only, no signal or delivery ids — so the
tooltips read "from this scan". A counter of `0` renders as plain text: linking
to a page guaranteed to be empty is worse than not linking at all.

#### What the scan form asks

The first question is **What this scan does**, and it decides the shape of the
rest of the form:

- **Catalog + monitoring** — adds events and fields to your tracking plan *and*
  records metric points, so anomalies and alerts can fire. A **Time column** and a
  **Schedule** are both required; the form will not save without them.
- **Catalog only** — adds events and fields to your tracking plan when you run
  it. No schedule, so no metric points, no anomalies and no alerts.

The difference between the two is the **schedule**, and only the schedule: a
config with no interval is never dispatched, which is exactly what catalog-only
means. The **Time column** is asked for in both modes because it does a second
job that has nothing to do with monitoring — it bounds every run to
**Limits → Lookback (hours)**. In Catalog only it is optional and unflagged
(*No time column — read the whole query* is a legitimate answer); leaving it
empty means each run reads everything the base query returns, which the Limits
section says in place of the lookback field. Choosing **Catalog only** never
clears a time column you already saved.

Only a monitoring scan produces metric points, so only a monitoring scan can
raise a signal or send an alert. See
[Concepts](concepts.md#monitoring-scan-vs-catalog-only-scan) for the definitions.

**Always visible (the essentials), in the order they appear:** the mode choice,
**Name**, **Data source**, **Base query** (used as a subquery), the **Load
preview** button, **Event type** and **Event type column**, **Time column**
(required in Catalog + monitoring, an optional run bound in Catalog only), — in
Catalog + monitoring only — **Schedule**, and finally the preview panel. The schedule is one of *Every 15 min* (`15m`),
*Every hour* (`1h`), *Every 6 hours* (`6h`), *Every day* (`1d`), or *Every week*
(`1w`).

**Event type** and **Event type column** are one question — where the name of
each event comes from — so they are asked together. Choose an event type and
every row becomes that event; leave it on *Name events from a column* and each
distinct value of the chosen column becomes its own event type. One of the two is
required in both modes: a config with neither cannot name anything, so no run of
it can ingest an event. **Create scan** and **Save** stay disabled until it is
answered, and the preview panel says the same thing rather than asking your
warehouse a question with no answer.

The event-type picker uses the project's main plan even when you are viewing a
plan branch. A scan writes catalog changes to main, so create, update and dry-run
reject an event type from a different project or a branch copy. Scan names must
also be unique for their data source; a duplicate name returns a conflict rather
than a server error. A malformed pre-release regex is rejected when the scan is
saved, instead of being ignored during collection.

**Everything else is a collapsed section.** Each carries one line saying what it
is for and what happens if you leave it alone. Editing a saved config opens any
section that already holds a non-default value.

| Section | What it is for | Contains |
| --- | --- | --- |
| **Event names and grouping** | Reshape the names tripl derives from the essentials — rewrite them from a template, collapse high-cardinality values, or merge several into one. Leave it alone and each name is used as it is. | Event name format · Cardinality threshold · Event groups · JSON values to keep as-is |
| **App version** | Attach an app release and platform to every event. Leave it alone if you do not ship versioned apps. | App version column · Platform column · Pre-release version pattern · Traffic share that counts as released |
| **Metric breakdowns and drift** *(Catalog + monitoring only)* | Extra columns to split metrics by, and columns whose value mix you want watched for drift. Leave it alone to collect one series per event. | Metric breakdowns · Value limit · Distribution drift |
| **Limits** | Caps on how much warehouse data each run reads. Leave them alone unless runs are slow or expensive. | Replay chunk size *(Catalog + monitoring only)* · Lookback (hours) *(needs a time column — with none, the section says each run reads the whole base query instead of offering the field)* · Row cap per run · Row cap per metrics run *(Catalog + monitoring only — a Catalog only scan has no metrics runs to cap; a cap set while monitoring is kept, not cleared, and returns if you switch back)* |

Sections that need your query's columns stay empty until a preview is loaded and
say so. Editing the base query (or pressing its Format button) keeps the JSON
value paths and drift fields you already chose; the next preview drops only the
ones whose column the new query no longer returns. A numeric limit the backend
would refuse — zero, a negative or a fraction, or a traffic share outside 0–1 —
is flagged under its field, and **Save** says which field to fix instead of
sending it. On a saved scan's **Configuration** tab there is one **Save** for
the whole form, at the foot of the sections; it is enabled once something has
changed. The shared number of releases to retain lives under **Settings → Project
→ General**. The platform column powers the platform-presence matrix. Reserved
role columns (event type, time, version, platform) cannot simultaneously be
selected as scalar breakdown/drift fields. The **Event name format** is the
exception that may name the **Event type column**: a column the format names is
never reserved away, and the row's own value fills the placeholder, so the
`{action}:{category}` shape the field's hint advertises works. The value goes
into the name only — that column still carries no per-event field value of its
own.

#### The preview panel

**Where:** the scan form, at the foot of the always-visible block — after the
fields the answer is computed from. One button, two halves.

1. **What this scan would create** — the dry run, described below. Event names,
   field names and the bounds the answer is under. This is what the panel leads
   with.
2. **Show sample rows** — the raw warehouse rows the query returned, collapsed.
   They are what the column pickers read, and they are useful evidence when the
   answer above surprises you, but they name neither an event nor a field, so
   they are no longer the headline.

The dry run describes one specific draft. Change the form after it ran — a
different event name format, a new group rule, a different cardinality threshold
— and the panel says the answer no longer describes this scan and offers
**Recalculate**, rather than leaving a stale list of event names on screen.

When a draft would add too many events — more than 25 new events, or names
built from more than three `column=value` segments — the answer carries a
warning to narrow the event name format or the grouping before the first run
writes them all into the plan.

A brand-new scan picks its **Event type column** from the very rows this button
loads, so on the first click there is often nothing yet that says how events are
named. The panel says so and asks nothing of your warehouse — *Nothing tells this
scan how to name events yet* — and once you answer, with an **Event type** or
that column, a **Show what this scan would create** button turns the same rows
into the answer.

#### The dry run — what this scan would create

Loading a preview also asks the backend "what would this config create?" and
answers with **event names and field names**, not raw rows. The answer is
computed by pushing the sampled warehouse rows through the *same* planner a real
run uses, so the names you see are the names a run would write — the event name
format, the group rules and the cardinality collapse are all applied for real.
Nothing is written: the planner is a pure function and the dry run holds no
transaction open on your plan.

It counts events the way a run creates them: one per **event type**, not one per
name. A scan that groups on an **Event type column** runs the planner once per
group, exactly as the real scan does, so if two event types both produce the
name `home` you get two entries — and each is labelled *new* or *already in your
plan* against its own event type.

It is deliberately bounded, and says so rather than rounding up. Three separate
partialities are reported independently:

| Bound | What it means | How it reads |
| --- | --- | --- |
| **Lookback window** | With a **Time column**, only rows inside the scan's lookback were read — an event absent from the last 24 hours is not an event that will not be created. With none there is no window, and the whole base query was read. | The summary names the window it used, or says *No time window — the whole base query was read*. |
| **Sample** | The dry run examines at most a fixed number of the *most common* column combinations (5,000 by default, `sample_row_limit`). If it hit that cap, more distinct events exist than it looked at. | "Would create **at least** N events", never a flat N. |
| **Event cap** | Generation stops at 10,000 events per pass. The real scan stops there too. | An explicit note when the cap was reached. |

It never projects a table-wide total. Each event carries its share of the sample
and an exact count of the sampled rows behind it — not an estimate of how many
rows exist in your warehouse.

Four more things it reports, each answering a question the raw rows could not:

- **Templated columns.** A column with more distinct values than the
  **Cardinality threshold** collapses into a `${column}` template, so you get one
  event instead of thousands. The dry run names the column and its distinct
  count, because that is a step function of a threshold you are editing on the
  same form, not a property of your data.
- **Event name format errors.** A format referencing a key the rows cannot supply
  fails *every* run of that config. Catching it here, instead of after two
  hundred failed production runs, is the single most valuable thing this feature
  does. The error is reported, not raised — the dry run still completes.
- **Rows with no derived name.** When every placeholder the **Event name
  format** names resolves to nothing for a row — a NULL column, or a JSON path
  that row does not carry — the name comes out empty. Such rows are **not
  planned** — the preview does not list an event for them, because the run will
  not create one — and the dry run reports the count as a warning:
  *Skipped N rows whose derived event name was empty* (singular *row* when N is
  1). A name with empty **segments** is a different thing and *is* still
  planned: `::` and `onboarding:start:` are real identities with a click target
  and a rendering of their own, and refusing them would leave real traffic
  unplanned.
- **Fields.** A field is either `json` or `string`. That is the entire type
  inference a scan performs; claiming `integer` or `timestamp` would be a claim
  about something the scan does not do. Fields are only reported as "would be
  added" on the event type column path, which is the path that
  creates them; with an explicit event type, columns the event type does not
  declare are listed as **unmapped** instead, because a run would skip them.
  The panel says so in those words — *a run only fills the fields this event
  type already declares* — and reserves *every column is already mapped* for the
  case where nothing is unmapped. When something is, a **Create N fields** button
  sits directly under the panel and declares exactly the columns it just listed
  on that event type; it never offers a reserved column, because it is driven by
  the same `unmapped_columns` answer rather than by a second reading of the
  preview.

Like the preview, the dry run runs free-text SQL against a stored credential, so
it is **owner-only**.

#### The mode badge

Every scan row and the scan detail header carry a badge derived from the two
columns the dispatcher reads:

| Badge | Meaning |
| --- | --- |
| **Monitoring** | Time column and schedule both set. Collects metric points on a schedule. |
| **Catalog only** | No schedule. Adds events and fields to your tracking plan; no metric points, no anomalies, no alerts. |
| **Needs a time column** | A schedule but **no time column**. The dispatcher never selects it, so the scheduler never runs it and it collects no metric points. Runs you start by hand still add events to your plan. Add a time column to fix it. This is the same finding the CLI reports as `scan_config_not_dispatchable`. |

The **Monitoring** tile at the top of the Scans page counts only the first of
these.

### Scan runs

Starting a scan creates a **run**. (The API and the CLI call the same record a
`job` — `tripl scans jobs`, `scan_job.*` stream events — but every screen in the
web UI says *run*.) From a config you can run a scan, apply event groups, jump
directly to **Review events**, or replay metrics over historical chunks (replay
requires a time column and an interval). Runs expose status, progress, and
curated failure detail. A run's **details** list flags warehouse
columns that carried data but had no matching field in the plan — a real
coverage gap worth fixing. Run, replay, and apply-groups requests refuse to
start a second live job for the same scan. Reaching a configured scan
row limit fails that run rather than returning a partial successful result;
successful summaries therefore do not carry a truncation flag. A stale Celery
connection-test task is no longer used: the data-source connection test runs
through the API request path.
It stays quiet about columns that were empty for
those rows, and about reserved role columns (event type, time, version,
platform, and any column an event-group rule matches on), which tripl already
uses elsewhere and never expects to have a plan field. A rule condition reserves
a column only when it names one outright: a dotted condition such as
`payload.action` reaches inside a column's JSON rather than claiming the column,
so it reserves nothing and `payload` keeps its field. The same list reports
variables the run retired — *Retired N unused variables no event refers to*,
see [Variables](#variables) — and says nothing when there were none. It also
reports rows the run refused to name: *Skipped N rows whose derived event name
was empty* (singular *row* when N is 1), which means every column the **Event
name format** refers to was NULL for those rows, so the run planned nothing for
them rather than creating a nameless event. Repeated identical failures collapse into
a streak with an expander, and **Run again** retries the config without losing
its history.

**Stopping a run.** **Stop run** is offered on every run that is still queued or
running, whatever kind it is, and a run you stop stays **Cancelled** — a metrics
run that goes on to finish the chunk it was in does not flip the row back to
*Succeeded*, and the metric points it had already written are kept. A catalog run
and an event-group apply each look once, at the last point before they commit:
stopped there they write nothing at all and the plan is exactly as it was.
Stopped after that — while the run is retiring variables or rebuilding the search
index — their work is already durable and it stays: the catalog a scan wrote and
the variables it retired, the merge an apply performed, and the rebuilt index in
either case. A stop cannot un-write a committed transaction. The **run** still
ends **Cancelled** even so. Before it stamps *Succeeded* it re-reads its own row,
and a stop that landed while those tails were running owns the verdict — so the
two halves come apart: the work survives, the run does not claim to have
succeeded. The **run report** is recorded either way, so you can still read what
the run managed to do before you stopped it.

**What period a replay may cover.** A replay period must end **at or before the
last completed interval**: the interval that is still filling holds no complete
bucket to replay. **Replay a period…** — in the scan page header, opening a
dialog — seeds exactly that — the period it
opens with ends on this scan's own last closed bucket and reaches at least 24
hours back — so its defaults are always accepted. A period reaching into the
interval still filling is refused when you submit it, with a message naming the
latest end that would be accepted, instead of being accepted and turning into a
failed run minutes later.

The scan list heads four figures: **Scans**, **Monitoring** (scans that have
both a time column and a schedule, so the dispatcher actually picks them up),
**Failing**, and **Warehouse rows · 24h**. The detail page adds **Scanned · last
run**, **Events written**, and **Metric points** — which also say when the last collection
landed and when the next is due, both read from the scheduler's own record
(`last_metrics_run_at` / `next_metrics_run_at` on `GET /projects/{slug}/scans/{scan_id}`)
rather than guessed from the newest run. On a monitoring scan the header's
chips add **Next run in …** (or **Next run due now**). Changing a scan is for
owners, so a viewer or editor opening the Configuration tab sees a read-only
list of its settings rather than a disabled form. The **Danger zone** on a scan's Configuration
tab holds only **Delete**; replays start from **Replay a period…** in the page
header. **Apply to existing events** sits on the Event mapping tab's **Event
group rules** row. The Configuration tab's save bar appears only once you have
edits, with **Discard** and **Save changes**. A project with no scans shows a
single empty state in place of the list.

**Warehouse rows · 24h** counts warehouse rows only: those metrics runs read,
and those behind a newer catalog run's breakdown. An older catalog run reports
only the grouped column combinations it read back — a different unit — so
those are not added in; the figure's hover title names them separately
("Catalog runs that report no warehouse rows also read back 153 column
combinations"). The activity endpoint returns the two sums as
`warehouse_rows_24h` and `catalog_combinations_24h`; the older mixed
`rows_read_24h` is their total.

The 24h figure, and the **failed last N runs** tag on a scan's collapsed
failures under **Recent runs**, are exact: the server counts them over each
scan's whole history (`GET /projects/{slug}/scans/activity`), so a scan that
runs every few minutes no longer shows them as floors ending in `+`. A run
counts toward the 24 hours by when it finished, or when it started if it is
still running.

The **Limits** hints quote the instance's real row caps for a scan with none of
its own, read from `GET /api/v1/settings/row-limits` (open to every signed-in
user); until that answers they quote the shipped defaults, 50,000 and 100,000.

**Metric points**, not "metric rows": these are points on a metric time series —
what anomaly detection and alerts are built on — and *Metrics* is the name of a
different surface (Observe › Metrics, the catalog of user-defined metrics). The
figure sums all four counters a run reports (per-event, per-type, and their
breakdown variants), so the list and the detail page always show the same number
for the same run.

**Scanned** counts what a run read — bounded by the row caps below — not rows
written into your plan. A metrics run shows the warehouse rows read across
every metrics chunk (bounded by **Row cap per metrics run**). A catalog run shows
the warehouse rows behind the column combinations it read back, with the
combinations beside them — *Read 48,200 warehouse rows (153 distinct column
combinations, grouped in the warehouse)* — bounded by **Row cap per run**. A
catalog run from before that count existed still shows only its combinations
("153 combos"), so the label spans **two populations**. A
column header cannot vary per row, so each figure — the stat card and each cell
in the run table — carries a hover title saying which of the two it is.

#### What a run report says

Expanding a run leads with **What this run did**: plain sentences about your
data, in this order, each omitted when the run has nothing to report for it.

| Line | What it means |
| --- | --- |
| *Read N warehouse rows.* | Rows the run read from your warehouse. A replay adds *across N chunks*; a catalog run adds the column combinations they grouped into, *(M distinct column combinations, grouped in the warehouse)*, and an older catalog run reports only those. Hover for which cap applied. |
| *Added N events to your tracking plan.* | Events that did not exist in the plan and now do. |
| *No new events — all N were already in your plan.* | The run discovered nothing new. Normal on an established catalog, not a failure. |
| *N events were already in your plan and were left as they are.* | The old "Events skipped" counter, with its reason. Nothing was lost or overwritten; their field values were refreshed. |
| *Added N variables.* | Variables the run created from the values it saw. |
| *Looked at N columns in your query.* | Coverage, not a to-do list: how much of your query the run analyzed. |
| *Recorded N metric points.* | Time-series points written. Only a monitoring scan produces these. |
| *Raised N anomaly signals.* | Signals **this run** added. Links to [Anomalies](#anomalies) filtered to this scan. |
| *Queued N alerts.* | Alerts **this run** queued. Links to [Alerting](#alerting) filtered to this scan. |
| *Catalog-only scan — no metric points, so no signals and no alerts.* | Shown on a scan with no schedule, so a green run that produced nothing downstream does not read as a silent failure. |
| *This scan has a schedule but no time column, so the scheduler never runs it — no metric points, so no signals and no alerts. Add a time column to fix it.* | The same silence for the opposite reason: the run you are reading is a manual one, and the schedule above it is never dispatched. It gets its own sentence because calling it a catalog-only scan would name a mode its owner never picked. |

*Raised N anomaly signals* is that run's **delta** — signals the run added. The
**Anomalies** page it links to counts what is **open now** for that scan. The two
answer different questions and routinely disagree; both are correct. Where they
disagree the report puts the other number under the sentence — *5 signals from
this scan are open now*, or *None from this scan are open now* once they have
closed — and where they agree it says nothing, because there is nothing to
reconcile. The same delta is what the activity feed's "N new signals" reports on
a scan card.

Every counter the run reported is still there, verbatim, behind **Show raw
counters**: *Events created*, *Variables created*, *Variables retired* (on
every run but a replay, `0` included), *Events skipped*, *Columns analyzed*,
*Event breakdowns*, *Distribution rows*, *Signals added*, *Alerts queued* — and,
on a scheduled run, the variable-value sampling sweep: *Paths
sampled* (how many still-unobserved paths this run attempted), *Paths with
samples* (how many of those came back with a value — sampled high with zero
back is the signature of a failing sampling query), *Values written* (contexts
whose stored values it changed), and *Contexts unfilled* (what remains to
fill, falling run over run as sampling converges). Nothing was removed — the
sentences lead, the counters follow.

### Audit log

**Where:** Govern › Audit log — **owners only**; the nav item is hidden from
everyone else. A record of mutating actions on **this project**, filterable by
action, user and time range; actions that belong to no project (members, API
keys, a project's deletion) are in **Settings › Instance › Audit log**. Entries
are grouped under day headers, each reads as a sentence with chips for the
actor and the object, and an expanded entry shows its payload as labelled values
with a **Raw JSON** toggle. Each entry also records the
**plan branch** the write was scoped to, so two contradictory edits to the same
object on two branches are told apart. The rule is exact:

Instance settings changes record the names of changed fields without storing
their secret values. Scan runs and metrics replays record the resulting job ID;
branch comments and conflict resolutions, photo and annotation changes, and
project anomaly settings changes record the acting user as well.

- an entry written through `?branch=<working branch id>` carries that branch's id
  and name, and the row shows a **branch chip**;
- an entry with **no chip** was written on **main**, *or* is an action with no
  plan-branch dimension at all — alerting, scans, data sources, users, API keys.
  A blank branch is not an assertion of "main", which is why the row shows a chip
  or nothing rather than labelling anything. Passing main's own id counts as no
  branch — main is spelled as the absence of a branch, so the same write cannot
  render two ways.

The branch **name** is stored verbatim alongside the id, so the trail survives
deleting the branch: the id goes null, the name remains, and the row stays
readable.

This covers the plan writes that carry a branch — `event.*`, `field.*`,
`event_type.*`, `variable.*`, `meta_field.*`, `relation.*` and
`project.retire_unused_variables`. Events are recorded as `event.create`,
`event.bulk_create`, `event.update`, `event.bulk_update`, `event.delete` and
`event.bulk_delete`; a bulk route files one row per request, not one per event,
with the ids — and, for a delete, the names — in the row's payload, sampled to
the first 200 alongside the true count when the request was larger than that.
Retiring events from **Reconciliation → Dead events** is in the log too, filed
as `event.bulk_update`: it is the same write, so it files the same action, with
the ids and the chosen status in the payload. That is the action to filter on —
there is no separate archive action.

Events have a second surface, and the two answer different questions. The audit
log answers **who, what, when and on which branch**, and it survives the event:
an `event.delete` row still names what was deleted after the event and its
history are gone. **Per-event history** on the event's own detail page answers
the **before/after values** of `status`, `name`, `title`, `description` and
`sunset_at`, `superseded_by_event_id`, of `tags`, and of each field value (`field:<name>`) and meta value
(`meta:<name>`), opening with a `created` row that names who created the event
and when; it is removed with the event. Neither is a backup — an `event.delete` row
does not let you reconstruct the deleted event's field values, deliberately, as
a single field value may be 100 000 characters.

Two exclusions, both deliberate: **reordering** an event (drag-to-reorder, and
the move up/down control) is not recorded — it changes display order only, and
would file a row per drag. Events written by a **scan** are not recorded either:
a scan is not a user action, and a collection run that creates ten thousand
events would file ten thousand rows saying that nobody did anything. The log
covers events from this release forward — edits made before it are genuinely not
recorded anywhere and are not backfilled.

Writes made from **Reconciliation** *are* recorded, because each one is a person
deciding something:

- **Accepting** a shadow-event candidate files `event.create`, the same action
  authoring an event by hand files. The scan only *proposed* an identity;
  admitting it into the plan is your decision, and the event that results is
  indistinguishable from one you typed. The payload names the candidate, the
  scan that observed it and how much traffic it carried, which is what tells the
  two doors apart.
- **Dismissing** a candidate creates nothing, so it has an action of its own —
  `shadow_event.dismiss`, filed against the candidate rather than an event. The
  payload carries the identity's latest observed volume and the span it has been
  seen over. Worth recording precisely because nothing is left behind otherwise:
  the candidate row is removed with the scan that found it, taking `resolved by`
  with it.
- **Retiring dead events** files `event.bulk_update`, as above.

The rule behind all three: the action names *what now exists*, not the screen it
was done on. Accepting a candidate is not a different thing to search for than
authoring an event by hand, and there is no separate reconciliation vocabulary
to remember.

The **project itself** is in the log too — `project.create`, `project.update`
(renames included), `project.delete`, and `project.reset` for a demo re-seeded in
place. The delete row is the odd one out and the reason the rest exist: once a
project is gone, so is every per-project surface that could have answered for it,
so that row carries the name and slug in its own **payload** rather than relying
on an id that now resolves to nothing.

This tab lists one project's entries, and it asks for them by **project**, not by
name: the slug in the URL is resolved to the project it currently belongs to. So
renaming a project keeps its history in one place rather than splitting it at the
rename, and a slug freed by a deletion and then taken by a new project does not
hand the newcomer its predecessor's past.

Which leaves the entries with no project to answer to — a data source connected,
a member invited or given a role, a workspace API key minted, and a project's own
deletion, since a deleted project has no page left to open. Those live in
**Settings → Instance → Audit log**, the owner-only view of every entry on the
instance with no project filter at all. It is the same log read at a different
scope, so an entry appears in both places when it belongs to a project, and only
there when it does not.

A **cascade** is neither exclusion: it is recorded, but as the action that
caused it. Deleting an event type, and deleting, merging or reverting a plan
branch, each file their own row — `event_type.delete`, `plan_branch.delete`,
`plan_branch.merge`, `plan_branch.revert` — while the events destroyed with them
get no `event.*` row of their own. So the log tells you which action swept them
away, who ran it and when; it does not name the events it took. The count is on
screen before you commit to it: the event type's **Danger zone** states how many
events the delete will destroy, archived ones included.

Each entry keeps the request payload, which is why it is owner-gated: see
[Security](../run/security.md#roles-and-access-control-rbac).

---

## Cross-cutting tools

### Sign-in and password reset

The sign-in screen toggles between **Existing account** and **Create account**,
and exposes a **Forgot your password?** flow. After signing in you return to the
page you were sent from, query string included, so an alert link's incident
card or a branch link's branch survives the detour. If your session expires
while the app is open, a sign-in dialog opens over the page instead of
redirecting, so unsaved input is still there once you sign back in. Opening an
invitation or a password-reset link while signed in says which account is signed
in and offers **Sign out and continue**, keeping the link. Entering your account email
requests a reset; the screen then always shows the same neutral confirmation
regardless of whether that address is registered, so it can't be used to probe
for accounts. When the instance has email configured it sends a **single-use
reset link that expires in one hour** — opening it returns you to the sign-in
screen in "choose a new password" mode, where the new password must meet the
same policy as registration (at least 12 characters with a number and a symbol).
When email is **not** configured, the confirmation instead tells you to contact
your instance owner. Completing a reset also signs out the account's other
sessions. Password fields on the sign-in and invitation pages have a
show-password toggle. See **[Security](../run/security.md)** for the token and delivery
details.

### Command palette (⌘K)

Open with ⌘K / Ctrl+K (suppressed while you are typing in an input, textarea, or
contenteditable). It offers: **Navigate** commands (All projects, Data sources,
Members, Profile, and owner-only Runtime); the current project's own
destinations, built from the sidebar's nav model so every sidebar destination
has a row, grouped under the sidebar's **Plan** / **Observe** / **Govern**
headings and filtered by role exactly as the sidebar filters them, plus a
**More** group for the three that are not sidebar entries (Project settings,
Concepts, Detection settings); a **Projects** switcher; an
**Event types** jump list; **branch-aware knowledge search** (from 2 characters)
across events, event types, fields, meta fields, variables, relations, tags,
metrics, fact tables, scans and alert rules — events that differ only in one
naming-rule placeholder are folded into one row ("+ N variants") with a
**Show/Hide N variants** row that expands them in place (see
[Searching events](./searching-events.md)); the keyword answer is shown as soon
as it lands and the semantic re-ranking replaces it when that arrives; **Ask AI** (when AI is
enabled and the query is at least 8 characters, with cited sources); an
**Actions** group; and **Sign out**.

The **Actions** group starts common tasks rather than going to a page. Editors
get **New event**, **New metric** and **New branch** (which opens the
create-branch dialog on the Branches page) for the current project. Inside a
project there is one **Switch to** *branch name* row for main and for every open
branch you are not on; until the branch list has loaded, a single **Switch
branch…** row opens the Branches page instead. Owners get **Invite member**, which opens the Members page with the
invite form's email field focused. Everyone gets the light/dark theme toggle.

Inside the Settings takeover (`/settings/*`) ⌘K opens a **different, narrower
palette**. Those routes carry no project in scope, so this one offers only what
it can honestly reach: every settings section the rail lists (filtered by role
the same way), the projects by name, a **Project settings for &lt;name&gt;** row
per project, the way back out, and Sign out. There is no
knowledge search and no project-scoped destination — the takeover does not know
which project you came from well enough to search it. Leaving through any of its
rows goes through the same unsaved-changes guard as the rail links, so a draft is
never dropped silently, and Esc hands focus back to whatever opened it.

The rail's exit reads **Back to &lt;project name&gt;** — or **Back to workspace**
when no project is bound — and goes to that project's Overview. The rail's
Project label is a project switcher. Under **API keys**, revoked keys fold
behind **Show revoked (N)**, and the dialog that reveals a new key names it and
shows the `Authorization` header to send it in. **Members** is three cards:
invite, pending invitations, and the roster. Where a Get-started step sent you
here (Connect a data source), a **Back to checklist** bar sits above the
section.

That guard now covers **every** way out of the takeover: the rail, this palette,
the Back link, Sign out, the browser's **Back and Forward buttons**, and — as
the browser's own prompt rather than ours — reload and closing the tab. A
destination that keeps the draft, like moving between two Instance sections,
passes without a word. Opening a rail link in a new tab is not a leave at all:
the draft stays exactly where it is.

The authoring pages outside the takeover ask the same question. The event,
bulk-event, metric and fact-table forms, the new-scan page, a scan's
**Configuration** tab (switching to **Overview** included) and the event-type
field page (switching to another tab of the event type included) all ask before
a link, the sidebar, switching branch in the sidebar, **Back**, **Cancel** or
the back chevron drops edits you have not saved; reload and closing the tab get
the browser's own prompt. The alert rule, alert destination and data-source
dialogs ask before **Esc**, a click outside or **Cancel** closes them with your
changes in them, and the data-source edit dialog, which has an address of its
own, asks before **Back** too. A form you have not changed, or have changed
back, never asks.

The two halves of the app-wide palette's list narrow differently, and on purpose.

The **menu rows** — Navigate, the project's own groups, Projects, Event types,
Sign out —
are matched on a plain substring of what you have typed, against both the label
and the grey hint beside it (a route, a project slug, an event type's raw name).
So a project is findable by its slug as well as its name, but the match is
literal: `detection` finds *Detection settings* and `detset` does not. A group
whose rows have all been narrowed away takes its heading with it.

The **knowledge results** are ranked by the search service and are shown in
exactly the order it returned them: best match first, grouped by type, strongest
group first. Nothing is re-scored or re-sorted in the browser, so what you see is
the ranking the server computed — including semantic hits, which a literal
in-browser match would have pushed down or dropped. One consequence
of keeping each type together: a result can sit above a slightly stronger one of
a *different* type, when the weaker one's type opened higher up the list.

A row carrying a **`semantic`** chip is one the **keyword ranking did not
surface** — it is there because the meaning index found it, and the chip's
tooltip says *Found by meaning — the keyword ranking didn't surface this*. The
keyword leg is itself a capped scan, so a weak keyword match crowded out of it
can come back through the meaning index and carry the chip: the chip says which
leg put the row in front of you, not that the word is absent from it. So a row
whose name IS what you typed does not carry the chip, however certain the match:
the chip is about how the row was
found, not how good it is. Nothing changes about the ranking; the chip only
explains rows that would otherwise look like they arrived for no reason.

While the **first** search of a palette session is running the list shows a
**Searching knowledge…** group. After that there are already rows on screen, and
they stay there: the previous query's results remain visible and selectable,
dimmed under an **Updating results…** line, until the new ones arrive — so the
list narrows instead of blinking empty, and Enter always goes to something the
reader can see. If a search comes back with nothing you get **No knowledge
matches** under the query you
typed; and if the request fails you are told the search *failed*, which is not
the same statement as "there is nothing there". When a query matches no menu row
and is too short to search on (one character), the list simply reads **No
matches.**

### Activity feed

A toggleable live panel titled **Activity** (opened and closed from the top
bar's **Activity** button; it used to read "Now") of recent activity for the project,
or workspace-wide when no project is in scope. It shows up to 20 items of type
`anomaly`, `scan`, `alert`, or `event`, severity-colored, auto-refreshing roughly
every 60 seconds, with a manual refresh. An `anomaly` item, from a scan or a
catalog metric, is dated by the bucket it describes, not by when detection last
re-scored it. It stays in the feed while that bucket is within the last 7 days,
or within 3 intervals of the series' own grid when that is longer. A weekly
series therefore stays visible: its newest reportable anomaly already starts
more than a week back, because the current week is still settling.
A completed `scan` item summarizes what
the run produced — new events, metric points, **new** signals, and how much it
read: **N column combinations** for a catalog run, **N rows scanned** for a
metrics run; every figure on the card is that run's delta, not a project total — and
reads "no new events discovered" when a run on an established catalog finds
nothing new (which is normal, not a failure) rather than a bare "0 events".
The signal figure is that run's own scan and nothing else: it compares the
scan's open signals before and after the run, so it answers *what this run
changed* rather than *what is open in the project*. Within that scan's event
scopes it classifies by exactly the rule the Anomalies page uses, freshness
floor included ([From a detected anomaly to a
signal](./anomaly-detection.md#from-a-detected-anomaly-to-a-signal)), so a run on
a daily or weekly scan is not measured against a shorter window than the page.
Catalog-metric signals belong to the project rather than to a scan and are never
counted on a scan card; the **Anomalies** page and the sidebar badge are where
those are reported.
A burst of same-type items from one scan — for example the events a single scan
implements, which all share the scan's timestamp — collapses into one summary
row ("N events implemented") that expands on click to reveal the individual
items, so a scan no longer floods the feed with near-duplicate rows. When there
is no recent activity the rail narrows and drops its footer instead of holding a
full-width empty column.

### AI-assisted features

Optional. Enable at Workspace settings › Instance › **AI** (route
`/settings/instance/ai`) with a provider and API key; a status endpoint gates the
AI UI. When enabled: **Suggest with AI** drafts an event description on the event
form, and **Ask AI** answers questions in the command palette with linked
sources. Search indexing (and optional embeddings) is rebuilt from Workspace
settings › Project › **General** via the **Rebuild index** action, which reports
documents indexed and whether embeddings were queued.

### Data sources & connection test

**Where:** Workspace settings › Data sources (owner only). Supported types and
default ports: **ClickHouse** (8123), **PostgreSQL** (5432, **version 14+
required**), and **BigQuery** (project/dataset based). Create, edit, and delete
sources; **Test connection** (the new-source dialog can test the connection
before you create it, and nothing is stored by that test; a new source is tested
again as soon as it is created, and an edited one when its host, credentials or
TLS settings change); browse the schema (tables/columns) for the scan
query builder; and view ingestion stats. Health is shown as healthy / stale /
failing / untested. Each connection card has a **Used by** line naming the scans
that read it, each linking to its scan page (with its project when it sits in
another one, and *and N more* past the first few), or **Not used by any scan**.
Deleting a source that scans read asks you to type its name first, and the
confirmation says how many scans and scan runs are removed with it, naming the
scans when there are three or fewer: *1 scan (Demo scan) and its 42 runs will be
removed with it.* The dialog checks secrets before saving: a BigQuery key must
be valid JSON with `"type": "service_account"` (paste it or **load the key
file**), and PostgreSQL certificates and keys must be PEM blocks
(`-----BEGIN …-----` to `-----END …-----`), not file paths. Credential fields
are excluded from browser autofill, password managers and spell check.

**Runtime controls.** Every source takes a **query timeout** (default 300s),
applied to the connect handshake and to the query itself. Per-warehouse
connection settings, shown only for the warehouse they apply to:

| Warehouse | Settings |
| --- | --- |
| ClickHouse | JSON path discovery mode (`dynamic` / `all`) |
| PostgreSQL | SSL mode, CA certificate, client certificate, client private key (PEM content; the key is stored encrypted and never returned), search path |
| BigQuery | Location, max billed bytes (cost guard, default 100 GiB), dataset allowlist (schema-browse scope) |

:::info Not interchangeable — read the capability matrix
The three warehouses expose the same features but not the same guarantees.
ClickHouse and PostgreSQL are verified by **executing** tripl's generated SQL
against real servers in CI. BigQuery is verified by Google's real ZetaSQL
analyzer, which proves the SQL is **valid** but never checks a computed **value**.
Supported time-column types, nested-JSON behavior, TLS defaults and minimum
versions also differ.

See the **[warehouse capability matrix](../develop/warehouse-parity.md)** for the
per-capability proven/believed/bounded breakdown, setup requirements, permissions,
dialect-correct SQL examples and troubleshooting.
:::

---

## Related pages

- [Concepts](./concepts.md)
- [User Guide](./user-guide.md)
- [Troubleshooting](./troubleshooting.md)
- [Agent / API Guide](../integrate/agent-api-guide.md)
- [Configuration](../run/configuration.md)
