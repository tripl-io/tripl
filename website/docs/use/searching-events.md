---
title: Searching events
sidebar_position: 8
---

# Searching events

Press ⌘K (Ctrl+K on Windows and Linux) anywhere inside a project and type at
least two characters. Alongside the pages and commands, the palette searches the
project's catalog on the branch you are on: events, event types, fields, meta
fields, properties, relations, tags, metrics, fact tables, scans and alert rules.
Results come back best match first, grouped by type. The
[feature reference](./feature-reference.md) describes the rest of the palette.

## Variants are folded into one row

A scan that names events from a data column makes one event per value it
sees. A `screen` column gives you `event_name=Screen View | screen=Home`,
`event_name=Screen View | screen=Map`, `event_name=Screen View | screen=Cart`,
and so on. Search for "screen view" and every one of them matches equally well.
Listed one by one, they pushed everything else off the list and looked like
duplicates.

The palette now shows such a family as **one row**: the best-ranked member,
followed by **+ N variants**. Right below it, a **Show N variants** row expands
the rest in place. Select any member to open that event. **Hide N variants**
folds them away again.

Two events count as variants of each other when both of these hold:

- they belong to the **same event type**, and
- their names differ in the value of **exactly one** placeholder of the naming
  rule that produced them.

The naming rules are the project's scan **event name formats** (such as
`Screen View: {screen} ({platform})`), plus the default
`column=value | column=value` name a scan writes when it has no format. Some
examples:

| Names | Folded? |
| --- | --- |
| `screen=Home \| action=tap` and `screen=Map \| action=tap` | Yes: only `screen` differs |
| `screen=Home \| action=tap` and `screen=Map \| action=swipe` | No: two placeholders differ |
| `screen=Home` in *Screen view* and `screen=Map` in *Click* | No: different event types |
| `Home Screen View` and `event_name=Home Screen View \| screen=Home` | No: the first is a hand-written name that no rule produced |

When a name could vary on more than one placeholder, the one that gathers the
most matching results wins. A format that cannot be read back reliably is never
used to fold anything. That covers a bare `{screen}` with no fixed text, two
placeholders with nothing between them, and a placeholder used twice. A name
whose value contains the format's own fixed text is left unfolded as well: with
`{screen} | {action}`, the name `A | B | Click` could be screen `A` or screen
`A | B`, so tripl does not guess.

Folding only changes how results are listed. Ranking stays the same: the row
sits where its best member ranked, and the members inside it keep their order.
The palette asks for 12 rows, and a folded family counts as one of them, so
other results are not squeezed out.

## Over the API

`GET /api/v1/projects/{slug}/search` folds variants only when you pass
`group_variants=true`. The default is `false`, so existing callers get
the same answer as before. With it on:

- the representative result carries a `variant_group` object:
  - `key`: stable for the same family across searches
  - `pattern`: the name with the varying value shown as `{placeholder}`
  - `placeholder`
  - `count`: every member, representative included. Members are gathered
    from the top 100 matches only, so when `truncated` is `true` the count is a
    lower bound
  - `variants`: the other members, best-ranked first, each with its `id`,
    `event_id`, `title`, the `value` it substituted, `route_path`, `score` and
    `confidence`
- `limit`, `total` and `truncated` count **rows**, and a folded family is one
  row. `truncated` is also `true` when more matches exist past those top 100,
  even if the folded rows fit on the page.

Every other result has `variant_group: null`.

## Search by meaning, per organization

Besides matching words, search can also match by **meaning** when semantic
search is on for your organization. Each organization chooses its own
embedding provider under **Settings → Organization → Semantic search**: an
OpenAI-compatible endpoint, model and API key, or the operator's, which it
inherits by default. Your projects' text is only ever sent to your
organization's endpoint, and a query is only compared with vectors your
organization's endpoint and model produced. Another organization's vectors are
never compared with yours.

- **Changing the model or endpoint** re-embeds your organization's projects in
  the background. Until that finishes, results come from word matching plus
  whatever is already embedded with the new model.
- **With semantic search off** (switched off, or the operator runs with
  `ORG_SETTINGS_OPERATOR_FALLBACK=none` and your organization has no endpoint
  of its own), search still works and ranks by word matching. Over the API the
  response reports `semantic_used: false`.
- The **demo project** keeps its built-in example vectors while semantic search
  is off, so meaning search can be tried without a key.

Owners and admins see where each value comes from and can go back to the
operator's with **Inherit**. The model must produce 1536-value vectors; a model
that answers with another size is refused when you save it. See
[Operator and organization settings](../run/configuration.md#operator-and-organization-settings).
