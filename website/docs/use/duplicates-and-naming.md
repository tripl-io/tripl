---
title: Duplicates & naming
sidebar_position: 11
---

# Duplicates & naming

Catalogs rot in two quiet ways: near-duplicates (`paywall_view` next to
`paywall_screen_view`, both counting the same screen) and names that drift away
from the project's own style (`checkoutStarted` in a catalog of
`checkout_completed`). tripl checks both while you add events, and keeps a
**Duplicates** view of the pairs that already slipped in.

Everything on this page is computed from the names, descriptions and field
values already in your plan. **No language model is involved**: no prompt is
sent anywhere, and the same input always gives the same answer.

For where each hint appears in the app, see the
[Feature Reference](./feature-reference.md#duplicates). For the API, see the
[Agent API Guide](../integrate/agent-api-guide.md#duplicates-and-naming).

## Warn, don't block

A duplicate warning or a lint hint never stops a create. They are advice: the
form, the bulk create, the shadow inbox and the scan preview all still go
through when a hint is showing. The one refusal that already existed is
unchanged: two events cannot share a scan **identity** in one event type
(see [Event detail & editing](./feature-reference.md#event-detail--editing)).
That is an exact-match rule; this page is about the near misses it cannot see.

## How similarity is scored

### Normalising a name

Before two names are compared, each is split into lowercase tokens. A split
happens at every character that is not a letter or a digit (underscore, hyphen,
space, colon, dot, slash), at a case change (`paywallView`) and between letters
and digits (`step1`), so `Paywall View`, `paywall_view`, `paywall-view`,
`paywallView` and `paywall:view` all become the tokens `paywall`, `view`. A
single lowercase letter that opens a word stays attached to it, so `iOS` and
`iPhone` are one token each. Letters outside ASCII are handled the same way. No
token is thrown away, not even common ones such as `screen`.

### The lexical score

Two normalised names get a **lexical score** between 0 and 1. It starts as the
higher of two measures:

- **token overlap** (Jaccard): shared tokens divided by all distinct tokens of
  the two names, which catches the same words spelled in another style; and
- **character trigram similarity** (Dice): shared three-letter fragments,
  which catches typos, plurals and glued words (`signup` vs `sign_up`).

Three rules then adjust it:

- **Added filler words.** `screen`, `page`, `button`, `btn`, `event`, `the`,
  `a`, `an`, `on`, `of` and `to` are filler. When two names have the same other
  words and one of them only **adds** filler, the score is raised to **0.95**:
  `paywall_view` and `paywall_screen_view` score 0.95. When the filler is
  **swapped** instead, nothing is raised: `Paywall Screen View` and
  `Paywall Button View` name two things and score 0.65.
- **Word order.** When the same words (filler aside) appear in a different
  order, the score is capped at **0.75**: `home_to_profile` and
  `profile_to_home` score 0.75. The exception is when the only word that moved
  is an action verb, so `view_paywall` and `paywall_view` still score 1.0.
  Verbs that are just as often nouns (`purchase`, `search`, `share`, `rate`,
  `like`) and participles (`done`, `completed`) do not count as movable here.
- **Numbers.** When each name has a number the other lacks
  (`onboarding_step_1` vs `onboarding_step_2`), the score is capped at **0.5**:
  those are distinct events by construction.

### Names built by a naming rule

When an event type's scan has a name rule (`event_name_format`, for example
`{category}:{action}:{label}`), two names of that type are compared **slot by
slot**. The rule is read as a pattern: every piece of literal text between two
placeholders is a separator, whatever it is (`:`, `_`, `-`, a space, or a longer
string such as ` - `), and matching is case-insensitive. Placeholders with
nothing between them (`{a}{b}`) cannot be told apart and count as one slot.

Each slot pair gets its own lexical score, and the pair's score is the
**lowest** of them: under a rule every slot is part of the identity, so
`shop:tap:filter` and `shop:tap:filters` are two events that differ in one
value and score 0.8, where the same two names without a rule score 0.91. When
either name does not follow the rule, the whole names are compared instead.

### Adding embeddings when they exist

If your deployment has semantic search switched on
(`SEARCH_EMBEDDINGS_ENABLED`, see [AI & search](../run/ai-and-search.md)), each
event already has an embedding vector for search. The duplicate check reuses
the stored vectors; for the names being checked it embeds each candidate once
(only for up to 50 candidates per request, with a 3-second limit). The
**combined score** is then

```text
combined = max(lexical, 0.5 × lexical + 0.5 × cosine similarity)
```

so an embedding can only add evidence, never hide a lexical match. Two limits
keep it from inventing duplicates:

- **The lexical gate.** A pair is only considered at all when its lexical score
  is at least **0.76**, the lowest score a perfect cosine could lift to the
  threshold. Names that share no letters are never matched, whatever their
  embeddings say.
- **Slot-by-slot pairs get no help.** A pair compared slot by slot under a
  naming rule must reach the threshold on its lexical score alone; the vectors
  of two rule-built names are near-identical and would merge events that differ
  in one value.

Without embeddings, when the provider does not answer, or for an event that has
not been embedded yet, the combined score is simply the lexical score. So the
feature works on every install.

### The threshold

A pair counts as a likely duplicate when its combined score is **0.88 or
higher**. The threshold is the same for every project for now.

### Who is compared with whom

A name being checked is compared against the **non-archived events on the same
branch**, of every event type. Matches of the **same event type** are listed
first, then matches of other types, each group by score. A match of another
type is always compared as a whole name, never slot by slot. At most **three**
matches are returned per name.

Each match carries its reasons, from this list:

| Reason | When |
| --- | --- |
| `same name` | The two names normalise to the same tokens. |
| `similar name` | Otherwise, when the lexical score alone reaches the threshold. |
| `semantic match` | The embeddings' cosine similarity is 0.85 or higher. |
| `same event type` / `different event type` | Always one of the two. |
| `N shared field value(s)` | The check was sent field values, and N of them (field and value) are also set on the existing event. |

## Naming conventions and lint

### How the convention is inferred

tripl reads the names already in your catalog and infers the project's style.
It learns from the `live` and `implemented` events of event types **without** a
naming rule (a rule spells names from warehouse values, which says nothing about
how people name events), falling back to every non-archived event of those types
when fewer than five are live or implemented.

| Aspect | What is inferred |
| --- | --- |
| **Case** | `snake` (`checkout_started`), `camel` (`checkoutStarted`), `pascal` (`CheckoutStarted`), `kebab` (`checkout-started`), `space` (`Checkout Started`, with title, lower or sentence case), or `mixed`. A style needs at least 60% of at least five names that show one; a catalog of mostly single lowercase words counts as `snake`. |
| **Separator** | `:`, `.`, `/` or `\|` when at least 70% of the names contain it, as in `checkout:started`. |
| **Verb position** | Whether the action word comes `first` (`view_paywall`) or `last` (`paywall_view`), or `unknown`. It needs 60% of at least three names that show one. Verbs come from a small built-in English list (`open`, `view`, `click`, `tap`, `show`, `start`, `complete`, `submit`, `select` and similar). |
| **Prefix per event type** | The first part (or first word) that at least 70% of an event type's names share, counted only from five names up. A leading verb is word order, not a prefix, and is never taken as one. |

A catalog with no clear style (fewer than five names, or a real mix) gets few or
no hints: tripl does not invent a convention the project does not follow.

### Lint codes

A name that departs from the convention gets one hint per problem, each with a
code, a short message and a suggested name:

| Code | Raised when | Example |
| --- | --- | --- |
| `case` | The name uses a different case style. | `checkoutStarted` in a `snake` catalog; suggests `checkout_started`. |
| `separator` | The parts are joined with a different separator than the catalog uses. | `checkout.started` where names read `checkout:started`. |
| `verb_order` | The action word is on the other side. | `view_paywall` where names end in the verb; suggests `paywall_view`. |
| `prefix` | The event type has a shared prefix and the name lacks it. | `started` in a type whose names begin with `checkout_`. |

Only unambiguous verbs are moved: `purchase`, `search`, `share`, `rate` and
`like` are as often nouns, and a name that starts or ends with a participle
(`Signup Done`, `Tutorial Completed`) describes a state, so neither gets a
`verb_order` hint. Suggestions keep acronyms and mixed-case words as written
(`HTTP`, `iOS`) except in `snake` and `kebab` case.

No lint is given for an event type with a naming rule (the rule, not the
author, decides the spelling), nor for a name that exactly matches an event
already in the catalog.

A single suggested name fixes all of a name's hints at once; **Use suggested
name** puts it in the name field.

## Where the hints appear

- **The event form** (**New event**): as you type the name or fill in the field
  values, the check reruns after a short pause (400 ms). Under a naming rule the
  name the rule builds is checked. Each match reads
  *Looks like **paywall_view** (94%) — Open · Mark as replacement*, followed by
  its reasons. The percentage is rounded down, so 87.9% never reads as the 88%
  it did not reach. **Open** goes to the existing event; **Mark as
  replacement** is for when the new event is meant to take over from the old
  one. Lint hints appear under the name, each as its message, then
  *Suggested: `paywall_view`* with **Use suggested name** (not offered when a
  naming rule writes the name).
- **Mark as replacement** turns the match list into one line, *Replaces "…":
  once this event is created, that one is deprecated with this one as its
  successor.*, with **Undo**. The save bar repeats it next to the button:
  *Creating this event also deprecates …, with this event as its successor.*
  The mark is dropped when you change the event type, or when the latest check
  no longer lists the marked event. After the create, the marked event is
  deprecated through the ordinary event edit.
- **Add many events…**: every line in the preview is checked in one request.
  A line that would be created shows its best match and its first lint hint in
  one compact line; there is no **Mark as replacement** there. It never changes
  the line's status: a warned line is still `will be created`.
- **The shadow events inbox** (Govern › Reconciliation): **Accept** first checks
  the candidate. If it looks like an event already in the plan, a dialog
  *Accept a possible duplicate?* lists the matches, and you choose **Accept
  anyway** or cancel. If the check fails, the accept goes ahead as before.
- **The scan preview**: see
  [Scan dry run warnings](#scan-dry-run-warnings) below.

Screen readers hear one summary per form or table, such as "2 possible
duplicates", rather than every match row.

Anyone who can see the project gets the hints, viewers included: checking a name
reads the catalog and writes nothing.

## The Duplicates view

**Govern › Duplicates** (`/p/<project>/duplicates`) lists the likely duplicate
clusters already in the catalog. It looks at events that are `live`,
`implemented` or `ready_for_dev` on the current branch and compares **only
events of the same event type**, scored the same way as above. Pairs at or above
the threshold are grouped into **clusters**: if A looks like B and B looks like
C, all three are one cluster.

Each cluster's heading reads *3 events · 93% alike*, where the percentage is the
strongest pair inside it. Each event shows its status and 7-day volume
(*1,234 in 7 days*, or *no data in 7 days*). The page proposes one event to keep:
the one with the most volume, then the most advanced status. Pick another with
its **Keep** radio button. Clusters come 25 at a time, strongest first, and
**Load more** fetches the next ones. A cluster lists at most 20 events.

The clustering reads at most 5,000 events and scores at most 200,000 pairs. On a
larger catalog the answer is partial, and the API says so (`truncated`).

For each event other than the kept one:

- **Open** goes to the event.
- **Set successor & deprecate** is how you merge. After a confirmation that
  shows what still depends on the event, it is set to `deprecated` with the
  kept event as its **Replaced by**. This uses the ordinary event edit, so it
  goes through the same branch rules, dependency warnings and history as any
  other deprecation. See
  [Retiring an event](./feature-reference.md#retiring-an-event).
- **Not a duplicate** dismisses the pair of this event and the kept one. That
  pair no longer links a cluster in this project, on any branch (a branch copy
  is recorded against the main event it came from); the two can still meet in
  one cluster through a third event. The dismissal records who made it and when.

### What "merge" does and does not do

There is no separate merge operation. "Merge" here means **successor +
deprecate** and nothing more:

- the deprecated event keeps its history, field values and scan identity, and
  collection keeps matching it while its old name is still sent;
- **Replaced by** is documentation: no volume is moved from one event to the
  other, and no metric or alert rule is re-pointed. The
  [Used by](./dependencies-and-impact.md) warnings on the deprecate show what
  still reads the old event, so you can move those yourself;
- the [sunset watch](./feature-reference.md#sunset-watch) then tracks the
  retirement, and flags the successor if it goes quiet.

If the two names really are one event arriving under two identities, an
**event group rule** on the scan is what folds them into one; see
[Reconciliation](./feature-reference.md#reconciliation).

Viewers see the list; setting a successor and dismissing a pair need an editor.

## Scan dry run warnings

The scan preview's dry run
([what this scan would create](./feature-reference.md#the-dry-run--what-this-scan-would-create))
checks the names of the events the scan would add and returns what it finds as
`name_warnings`. The dry-run summary shows them above the event list.

- **`combinatorial_explosion`** is checked only when the scan has a name rule.
  It is raised when more than **50** new names under one event type are the
  same except in one slot of the rule, and that slot's distinct values still
  number no more than the scan's **cardinality threshold**. A column with more
  distinct values than the threshold already becomes a `${...}` template and
  cannot explode, so below it lowering the threshold (or dropping the slot from
  the name format) is the fix. A rule `{element}:{offer}` that would add
  `promo_banner_click:offer_001` through `promo_banner_click:offer_080` under a
  threshold of 100 is an example. The summary reads
  *80 new names under Promo differ only in {offer}
  (`promo_banner_click:*`)* with up to three sample names.
- **`duplicate`**: a new name that looks like an event of the same event type
  already on main, with the best match. The summary heads the list
  *2 new events look like one already in your plan*, and each line reads
  *paywall_screen_view looks like paywall_view (95%)*, with the existing
  event's name linking to its page. At most 200 new names are checked per dry
  run.

The dry run scores **lexically only**: it runs in the background worker and does
not call an embedding provider. Both warnings are best-effort. If the check
cannot run, the preview still completes without them; it never fails a dry run.
