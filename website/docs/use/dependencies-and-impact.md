---
title: Dependencies & impact
sidebar_position: 9
---

# Dependencies & impact

Before you delete an event, deprecate it, rename a field or retire a property,
tripl can tell you what else in the project depends on it: which metrics count
it, which alert rules filter on it, which relations and properties point at it.
The same answer appears in three places: the **Used by** section on an entity's
page, the warnings around a delete, deprecate, archive or rename, and the
**Impact** panel of a plan branch.

This page describes which dependencies tripl tracks, how sure it is of each one,
and what it does with them. For where each surface appears in the app, see the
[Feature Reference](./feature-reference.md#dependencies-and-impact). For the API,
see the [Agent API Guide](../integrate/agent-api-guide.md#dependencies-and-impact).

## Warn, don't block

A dependency is a **warning**. A delete, deprecate or rename that would leave a
metric or alert rule pointing at something that no longer exists still goes
through. The dialog lists the dependents so you can decide, and the server does
not refuse the change.

The warnings appear in these places:

- **Bulk event actions**: the bulk delete, archive and deprecate confirm dialogs
  on the events list.
- **Delete dialogs** for an event type, a field (on the event type page) and a
  property.
- **Delete dialogs** for a metric and a fact table. A fact table that metrics
  still read is refused with `409` when you confirm, as below.
- **Inline warnings** on the event form when you rename, deprecate or archive
  the event, and on the property form when you rename the property.

Fields have no rename action, so there is no field-rename warning; a field
rename only shows up as a rename in a plan branch's diff and its Impact panel.

The one exception is unchanged: **fact tables** still block. Deleting a fact
table that metrics read, unbinding its data source, or dropping a named filter
or column a metric uses is refused with a conflict that names the metrics, as
described under [Fact tables](./feature-reference.md#fact-tables). The
dependency view shows those same metrics, but it adds no new refusals anywhere
else.

## What counts as a dependency

A dependency is an edge between two entities. Every edge has a direction:

- **Downstream** (*Used by*): the things that would be affected if this entity
  changed. A metric that counts an event is downstream of that event.
- **Upstream** (*Uses*): the things this entity is built on. The event is
  upstream of that metric.

These are the edges tripl resolves:

| From | To | Why it is a dependency | Certainty |
|------|----|------------------------|-----------|
| Event | Metric | The metric uses the event in its event composition. | Direct |
| Event | Alert rule | The alert rule filters on the event, or is scoped to it. | Direct |
| Event | Event | The other event is marked *superseded by* this one. | Direct |
| Event type | Event | The event belongs to the type. | Direct |
| Event type | Alert rule | The alert rule filters on the event type. | Direct |
| Event type | Metric | The metric's composition selects the whole type. | Direct |
| Event type | Relation | The relation links a field of this type. | Direct |
| Event type | Scan | The scan is bound to the event type. | Direct |
| Event, event type, metric | Scan, detection settings | A detection override (the per-scope sensitivity the false-positive ratchet writes) is stored for it. | Direct |
| Field | Relation | The relation links the field. | Direct |
| Field | Property | The property is bound to the field by id. | Direct |
| Field | Event | The event's metric breakdown columns name the field. | Direct |
| Field | Metric | An event-composition metric over events of this type breaks down by the field, or uses it as its platform or app-version column. | Direct |
| Field | Scan | A scan bound to this event type names the field as a metric breakdown column, a distribution-drift column, its platform column or its app-version column. | Direct |
| Field | Scan | The same columns on a scan with no event type binding. | Possible |
| Field | Metric | A `fact` metric reads a fact-table column with the field's name. | Possible |
| Field | Metric, fact table | The field's name appears as an identifier or as a JSON-key literal in a `sql` metric's query, a metric's filter SQL, or a fact table's SQL, row filters or column list. | Possible |
| Field | Property | A property binding names a column with the field's name, without being bound to the field itself. | Possible |
| Property | Event, field | The property is bound to the field for an event, the event overrides the property's values, or its `${token}` appears in the event's field values or meta values. | Direct |
| Fact table | Metric | The metric reads the fact table (the same check that blocks fact-table edits). | Direct |
| Metric | Alert rule | The alert rule is scoped to the metric. | Direct |

Each edge carries a short sentence that says why it exists, such as *metric uses
event in its composition* or *property bound to field*, and the **Used by** list
shows that sentence next to the item.

### Direct and possible

Every edge is either **direct** or **possible**.

- **Direct** edges come from a stored reference: an id in a metric's
  composition, an alert rule's filter, a relation, a property binding, a
  *superseded by* link, a detection override, or a column name read in the
  entity's own scope (a breakdown, drift, platform or app-version column on a
  metric or scan tied to the field's event type). If the reference exists, the
  dependency is real.
- **Possible** edges are a match by name with no stored id tying them to this
  entity:
  - the field name as a whole identifier, or as a JSON-key literal such as
    `payload->>'user_id'`, in free SQL (`sql` metrics, metric filter SQL, fact
    tables and their row filters);
  - a fact table column, or a column a `fact` metric reads, with the same name;
  - a property binding that names a column with the field's name;
  - a scan column with the field's name on a scan not bound to any event type.

  tripl does not parse the query, so a match can be a coincidence: another
  table's column with the same name, or an alias. A column name built by string
  concatenation is not found at all.

Possible edges are marked in every list and never block anything, fact tables
included. Treat one as a prompt to open the query and check it.

### How far it looks

By default tripl looks one hop away: the things that use this entity directly.
When asked, it looks two hops: for example an event, the metrics that use it, and
the alert rules scoped to those metrics. It never goes further, so the result
stays a short list rather than a graph of the whole project.

The counts in a **Used by** header and in a warning's summary (*2 metrics and 1
alert rule*) count only direct, one-hop dependents. Possible matches and
second-hop items are listed too, but counted apart (*…, plus 1 fact table that
may use it*).

## On a plan branch

Dependencies are resolved on the branch you are on. A plan branch has its own
copies of events, event types, fields, properties and relations, so the **Used
by** list on a branch shows the branch's own relations and bindings. Metrics,
fact tables and alert rules are not copied into branches, so a branch entity's
metric and alert-rule dependents are found through its counterpart on `main`.

### The branch Impact panel

A branch's detail page has an **Impact** panel. It takes the branch's diff, keeps
the changes that can affect something else (deleted, deprecated, archived,
renamed or otherwise edited events, event types, fields and properties), and
lists what each of them touches downstream, for example *`checkout:completed` renamed: 2 metrics and 1
alert rule*. It reads the same rename pairing the diff shows, so a renamed event
counts as one rename, not as a deletion plus an addition. An entity edited in
place without being renamed, deprecated or archived (a field's type changed, an
event's breakdown columns changed) is listed as a **change**. Additions are left
out: nothing can depend on something that did not exist yet.

The panel is a review aid for the branch's reviewers and author. Like the rest
of this feature it warns and never blocks: a branch with impact items can still
be approved and merged.

## Who sees it

Dependencies are read-only and follow the project's membership: any member,
viewers included, can see **Used by** lists and the Impact panel. Only editors
see the warnings, since only they can delete, deprecate, archive or rename.
