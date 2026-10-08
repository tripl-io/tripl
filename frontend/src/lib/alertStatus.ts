/**
 * How an incident is spelled on the alerting Inbox: its status, what fired, how
 * big it was, and what an action just did.
 *
 * Deliberately a sibling of `statusLexicon.ts` rather than an entry in it: the
 * inbox statuses are an alerting-only family, and the reason / magnitude / copy
 * helpers below are card text, not a status→colour map. It obeys the SAME
 * colour key (see statusLexicon's header) and reuses `ChipTone`, so a status
 * cannot mean one thing here and another on the monitors page.
 *
 * Everything here is pure and takes its inputs explicitly, so the copy that
 * decides what an operator believes happened can be unit-tested without
 * rendering a card.
 */
import { formatNumber } from '@/lib/format'
import type { ChipTone } from '@/components/primitives/chip'
import type {
  AlertInboxAction,
  AlertInboxActionResponse,
  AlertInboxBulkAction,
  AlertInboxGroup,
  AlertInboxStatus,
  MetricScopeType,
} from '@/types'

import { formatDateTime } from './datetime'
import { formatPercentDelta } from './percentDelta'
import { countOf } from './plural'

/** A status rendered as a coloured word: its human label and canonical tone. */
export interface AlertStatusLexeme {
  label: string
  tone: ChipTone
}

/**
 * The five inbox statuses, as words a person reads.
 *
 * Before this the badge printed `{group.status}` raw, so one literally read
 * `false_positive`, and three of the five (`open`, `acknowledged`, `muted`)
 * shared a single grey outline — the operator could not tell an untouched
 * incident from one they had already snoozed.
 *
 * `open` stays NEUTRAL on purpose. 52 of 57 production groups are open, and
 * tinting 52 of 57 rows destroys the scanning benefit colour exists for. Colour
 * here marks the minority somebody has already made a decision about.
 */
export const ALERT_INBOX_STATUS: Record<AlertInboxStatus, AlertStatusLexeme> = {
  open: { label: 'Open', tone: 'neutral' },
  acknowledged: { label: 'Acknowledged', tone: 'info' },
  muted: { label: 'Muted', tone: 'warning' },
  resolved: { label: 'Resolved', tone: 'success' },
  // Not `danger`. This is a judgement about the DETECTOR, not a failure of
  // anything; it used to render as a red `destructive` badge, styling the one
  // deliberate human call on the page as an error.
  false_positive: { label: 'False positive', tone: 'accent' },
}

/** Filter order: the states an operator moves an incident THROUGH, in order. */
export const ALERT_INBOX_STATUSES: readonly AlertInboxStatus[] = [
  'open',
  'acknowledged',
  'muted',
  'resolved',
  'false_positive',
]

export function alertInboxStatusLabel(status: AlertInboxStatus): string {
  return ALERT_INBOX_STATUS[status].label
}

export function alertInboxStatusTone(status: AlertInboxStatus): ChipTone {
  return ALERT_INBOX_STATUS[status].tone
}

/**
 * "Handled" means somebody decided something — anything but `open`.
 *
 * Note this is a presentation split, not a behavioural one: the dispatcher
 * suppresses `acknowledged`, `resolved`, `muted` and `false_positive`
 * identically, and resets all but an in-force mute back to `open` once the
 * scope goes quiet.
 */
export function isHandledInboxStatus(status: AlertInboxStatus): boolean {
  return status !== 'open'
}

/**
 * How each scope kind is named on a card.
 *
 * Lower-case because these read inside a sentence-shaped chip ("drop · release
 * regression"), and count-based scopes are called "volume" rather than by their
 * table name: the chip answers "what KIND of signal is this", while the scope
 * name right below it already answers "of what".
 */
const SCOPE_KIND_LABEL: Record<MetricScopeType, string> = {
  project_total: 'project volume',
  event_type: 'event-type volume',
  event: 'volume',
  schema: 'schema drift',
  distribution: 'distribution drift',
  release_regression: 'release regression',
  metric: 'metric',
  variable_value_drift: 'value drift',
  source_freshness: 'data late',
  lifecycle: 'lifecycle',
  property_drift: 'property drift',
}

/**
 * How one scope kind is named, for a surface that names a single kind rather
 * than an incident's whole set.
 *
 * Exported so the inbox's scope-kind filter offers the SAME words the chip it
 * filters by shows. A picker that says "Event" over cards that say "volume"
 * makes the reader hold two vocabularies for one column.
 */
export function scopeKindLabel(scopeType: MetricScopeType): string {
  return SCOPE_KIND_LABEL[scopeType] ?? scopeType
}

/**
 * The drift kinds: a change of shape, with no rise or fall to report.
 *
 * Their rows still say `direction: 'spike'` — filed there so a rule's Spikes
 * toggle gates every drift the same way (`alerting_property_drift`) — and
 * carry a `percent_delta` that compares two windows' row counts, or none at
 * all, rather than the drift's size. Read as a direction and a delta, a
 * replayed distribution drift said "spike 0.0%".
 */
const DIRECTIONLESS_SCOPES: ReadonlySet<MetricScopeType> = new Set<MetricScopeType>([
  'schema',
  'distribution',
  'variable_value_drift',
  'property_drift',
])

/** Whether a signal of this kind has a direction and a relative change to show. */
export function scopeHasDirection(scopeType: MetricScopeType): boolean {
  return !DIRECTIONLESS_SCOPES.has(scopeType)
}

/**
 * "drop · release regression" — the one line that answers "why did the same
 * alert come back for a different reason?".
 *
 * Suppression is keyed on (scan, rule, scope, scope kind, direction), and the
 * card used to render three of those five. Production has one event appearing
 * twice, same rule and same scan and same direction, differing ONLY in scope
 * kind: `release_regression` ("this screen regressed in the new build") beside
 * `event` ("this event's volume dropped"). They rendered as near-identical
 * cards, so muting one and watching "it" come back was the expected outcome of
 * a UI that never showed the axis they differ on.
 *
 * All kinds are joined, not just the newest item's: a legacy group can mix
 * them, and describing a mixed incident by one member is how the mute's blast
 * radius gets mis-read.
 */
export function incidentReasonLabel(
  direction: AlertInboxGroup['direction'],
  scopeTypes: readonly MetricScopeType[],
): string {
  if (scopeTypes.length === 0) return direction
  const kinds = scopeTypes.map(type => SCOPE_KIND_LABEL[type] ?? type)
  return `${direction} · ${kinds.join(' + ')}`
}

/** The arrow that carries direction at a glance, ahead of the word. */
export function incidentDirectionGlyph(direction: AlertInboxGroup['direction']): string {
  return direction === 'drop' ? '↓' : '↑'
}

/**
 * A magnitude, group-separated: `88.318` reads as "88", `0.04` stays "0.04".
 *
 * `expected_count` is a rolling baseline, so it arrives as a float. Printing it
 * raw put "197 vs 88.318 expected" on every inbox card while the activity rail
 * 400px away said "42,280 actual vs 33,376 expected" — two formats for one kind
 * of number, and "88.318" is read as 88,318 by anyone used to a decimal comma.
 * The inbox card rounded neither, which is what this first fixed;
 * the sibling surfaces (AnomaliesPage, OverviewPage, the top bar, the projects
 * list, the replay dialog) each reached for `Math.round` on the baseline, and
 * all five now come through here so one incident cannot be spelled two ways
 * depending on which page is showing it.
 *
 * Rounding is value-aware, because not every magnitude here is a count of
 * events. `metric` is a first-class alert scope and its values are genuinely
 * sub-unit: a `%` catalog metric STORES A FRACTION (0.08 == 8%, see
 * `alert_templates`), and ratio / average / per-distinct-user metrics have no
 * ×100 to rescue them at all. These columns were migrated integer → float
 * precisely so those values survive (f9a0b1c2d3e4), so an unconditional
 * `Math.round` would print "0 vs 0 expected · -66.7%" — a card contradicting
 * both its own delta and the "actual=4%, expected=12%" message the operator is
 * holding. Below 1 the decimals ARE the number; at 1 and above the integer part
 * carries it and the decimals are baseline noise.
 *
 * Callers that have room for it should keep the unrounded value in a `title`.
 * Named for the domain rather than `formatCount`, which is already taken by
 * `ui/chart-format` for the axis-tick abbreviation ("380k") — the opposite
 * trade-off, and not one an incident row wants.
 */
export function formatIncidentCount(value: number): string {
  if (Math.abs(value) >= 1) return formatNumber(Math.round(value))
  // Significant digits, not fraction digits: a bare `toLocaleString()` caps at
  // three FRACTION digits, which turns a 0.000123 ratio back into "0".
  return formatNumber(value, { maximumSignificantDigits: 3 })
}

/**
 * "412 vs 1,010 expected · -59.2%" — what actually happened, on the card.
 *
 * Reaching this before meant clicking "Show what was sent", expanding a
 * delivery and reading the items table: two lazy fetches deep, so a blip and an
 * outage looked identical in the list.
 *
 * A zero baseline is stated in WORDS, never as "0%". The percent gate admits
 * anomalies with no baseline at all — a scope resuming after an outage, an
 * event firing for the first time — and `formatPercentDelta` is the one place
 * that decides how that is written, so the inbox cannot disagree with the
 * delivery item or with the alert message the reader is holding.
 */
export function incidentMagnitudeLabel(group: {
  actual_count: number
  expected_count: number
  percent_delta: number | null
}): string {
  const delta = formatPercentDelta(group.percent_delta, group.expected_count)
  const actual = formatIncidentCount(group.actual_count)
  if (group.expected_count <= 0) {
    return `${actual} actual, none expected · ${delta}`
  }
  return `${actual} vs ${formatIncidentCount(group.expected_count)} expected · ${delta}`
}

/**
 * The unrounded counts behind {@link incidentMagnitudeLabel}, for the tooltip.
 *
 * Rounding is right for reading the card at a glance; it is wrong for anyone
 * reconciling a baseline against the detector, so the precision is moved rather
 * than dropped.
 */
export function incidentMagnitudeTitle(group: {
  actual_count: number
  expected_count: number
}): string {
  return `Unrounded: ${group.actual_count} actual, ${group.expected_count} expected`
}

/**
 * "worst 92.4% in this group", or null when there is nothing extra to say.
 *
 * Only for multi-item groups: on a single-item group the worst magnitude IS the
 * magnitude line above it, and printing the same number twice reads as two
 * different facts.
 */
export function incidentWorstDeltaLabel(group: {
  item_count: number
  max_abs_percent_delta: number | null
}): string | null {
  if (group.item_count <= 1 || group.max_abs_percent_delta === null) return null
  return `worst ${group.max_abs_percent_delta.toFixed(1)}% in this group`
}

/**
 * "Closed 30 Jul 2026 by V. Denisov · firing again" — for an incident somebody
 * already dealt with and that came back.
 *
 * `_reopen_closed_incidents` resets any non-open state back to `open` once the
 * scope goes quiet. That is a deliberate, documented decision, but it was
 * invisible: `acted_at` shipped and rendered nowhere, so an incident closed
 * last week was pixel-identical to one nobody had ever seen.
 *
 * Worded "closed … firing again", not "reopened by the system": a user-clicked
 * Reopen also leaves a fresh `acted_at`, so the two are indistinguishable from
 * here. The latest delivery is named as the latest delivery rather than as
 * "since", because nothing in the payload records WHEN it resumed and inventing
 * that date would be the false precision this line exists to remove.
 */
export function priorDecisionLabel(group: {
  status: AlertInboxStatus
  acted_at: string | null
  acted_by_name: string | null
  latest_delivery_at: string
}): string | null {
  if (group.status !== 'open' || !group.acted_at) return null
  const who = group.acted_by_name ? ` by ${group.acted_by_name}` : ''
  return `Closed ${formatDateTime(group.acted_at)}${who} · firing again, latest ${formatDateTime(group.latest_delivery_at)}`
}

function nameList(names: readonly string[], fallback: string): string {
  return names.length > 0 ? names.join(', ') : fallback
}

/**
 * The confirmation that names the whole suppression key before anything is
 * silenced.
 *
 * The user's model of a mute is "not this thing again"; the system's is an
 * exact match on (scan, rule, scope, scope kind, direction). Every gap between
 * those two is an incident the operator believes they silenced and did not, so
 * the sentence spells all five parts and then says what is NOT covered.
 *
 * `mutedUntilIso` is nullable because the Inbox can mute with no end date at
 * all, and that case gets its own clause rather than a formatted
 * timestamp: `formatDateTime(null)` would print "Invalid Date" inside the one
 * sentence whose entire job is to state the blast radius before anything goes
 * quiet. The open-ended clause also names the way back, because an indefinite
 * mute never lapses server-side — its sort key is frozen, so the incident sinks
 * out of the 30-day window and the Muted filter becomes the only route to the
 * Unmute button that lifts it.
 */
/**
 * "until 20 Aug 2026 12:00", or the whole open-ended clause.
 *
 * Extracted so the single-incident confirmation and the bulk one below cannot
 * describe the SAME wire value differently. The open-ended branch
 * is a sentence rather than a phrase because it has to carry two facts a
 * timestamp carries implicitly: that the silence never lapses on its own, and
 * where the incident will be found afterwards — an indefinite mute freezes the
 * row's sort key, so it sinks out of the 30-day window and the Muted filter is
 * the only route back to the Unmute that lifts it.
 *
 * Never `formatDateTime(mutedUntilIso)` on the null branch: it prints "Invalid
 * Date" inside the one sentence whose entire job is to state a blast radius
 * before anything goes quiet.
 */
function muteDurationClause(mutedUntilIso: string | null): string {
  return mutedUntilIso
    ? `until ${formatDateTime(mutedUntilIso)}`
    : 'with no end date — it stays silenced until you unmute it, and you will '
      + 'find it again under the Muted filter'
}

export function muteConfirmMessage(
  group: AlertInboxGroup,
  mutedUntilIso: string | null,
): string {
  const scopes = nameList(group.scope_names, 'this scope')
  const reason = incidentReasonLabel(group.direction, group.scope_types)
  const scans = nameList(group.scan_names, 'this scan')
  const rules = nameList(group.rule_names, 'this rule')
  const duration = muteDurationClause(mutedUntilIso)
  return (
    `Silences "${reason}" on ${scopes}, from ${scans} via ${rules}, ` +
    `${duration}. Nothing else is silenced: the same scope ` +
    'moving the other way, a different kind of signal on it, another scan or ' +
    'another rule all still alert.'
  )
}

/**
 * The confirmation for the one action on the page that changes DETECTION.
 *
 * It writes permanent scope overrides, and until now carried no confirm at all
 * — only a `title` tooltip, on a 4px-gapped button that swapped places with
 * Mute between rows.
 */
export function falsePositiveConfirmMessage(group: AlertInboxGroup): string {
  const scopes = nameList(group.scope_names, 'this scope')
  return (
    `Closes this incident and permanently makes detection stricter on ${scopes} ` +
    '— nothing else. Undo under Settings › Monitoring › Scope overrides. Some ' +
    'signal kinds are not tuned this way; the result will say which scopes were ' +
    'actually changed.'
  )
}

/**
 * What just happened, said out loud.
 *
 * Success was announced nowhere: `onSuccess` discarded the response and only
 * invalidated, so a click on row 12 greyed out the list and then silently
 * un-greyed it.
 *
 * The false-positive wording is driven by `overrides_written`, never by
 * `group.scope_type`. The tooltip used to promise a permanent detection change
 * on every card, while the ratchet skips any scope kind outside
 * RATCHETABLE_SCOPE_TYPES — 10 of 57 production groups are release regressions,
 * where it writes nothing at all. And `scope_type` is only the
 * NEWEST item's, so a client-side guess would be wrong on mixed groups too.
 */
export function inboxActionSuccessMessage(
  action: AlertInboxAction,
  previousStatus: AlertInboxStatus,
  response: AlertInboxActionResponse,
): string {
  switch (action) {
    case 'acknowledge':
      return 'Acknowledged. It stays quiet until the scope goes quiet, then reopens.'
    case 'resolve':
      return 'Resolved. It stays quiet until the scope goes quiet, then reopens.'
    // A NULL `muted_until` on a mute that just succeeded is the open-ended
    // mute, not a missing value — the branch used to be an
    // unreachable bare "Muted." because the API required a timestamp. Left as
    // that one word it would be the only feedback for a permanent silence and
    // would read exactly like a seven-day snooze, so it says which one it is
    // and how it ends.
    case 'mute':
      return response.group.muted_until
        ? `Muted until ${formatDateTime(response.group.muted_until)}.`
        : 'Muted — no end date. It stays quiet until you unmute it.'
    case 'reopen':
      return previousStatus === 'muted' ? 'Unmuted — alerts resume.' : 'Reopened.'
    case 'note':
      return 'Note saved.'
    case 'false_positive': {
      const written = response.overrides_written ?? 0
      return written > 0
        ? `Marked as a false positive — tightened ${countOf(written, 'scope', 'scopes')}.`
        : 'Marked as a false positive — no scopes tightened, this kind of signal is not tuned this way.'
    }
  }
}

/*
 * ---------------------------------------------------------------------------
 * Acting on SEVERAL incidents at once. Everything below describes
 * a batch; everything above it describes one incident. The two are kept apart
 * deliberately rather than folded into count-aware variants of the same
 * functions — see `bulkInboxActionSuccessMessage` for why the sentences differ
 * by more than a plural.
 * ---------------------------------------------------------------------------
 */

/**
 * How many incidents are about to go quiet, said before any of them do.
 *
 * The single-incident confirmation ({@link muteConfirmMessage}) spells out the
 * whole five-part suppression key, because the user's model of a mute is "not
 * this thing again" and the system's is an exact match on (scan, rule, scope,
 * scope kind, direction). That sentence cannot be scaled up: at ten incidents
 * it is ten five-part keys, which nobody reads, and abbreviating it to the
 * union of the parts would state a blast radius that is WIDER than what is
 * actually silenced — the exact misreading the single sentence exists to stop.
 *
 * So the bulk sentence answers the question a bulk mute actually raises, which
 * the single one never has to: HOW MANY am I silencing? The count is the blast
 * radius here, because each selected incident keeps its own key and nothing
 * else is touched. The last clause says so, in the same words as the single
 * confirmation, so an operator who has read one recognises the other.
 *
 * `count`, not the groups themselves: this sentence deliberately names no
 * incident, so growing it a list later would be a decision someone makes on
 * purpose rather than a `.map()` that quietly appears.
 */
export function bulkMuteConfirmMessage(count: number, mutedUntilIso: string | null): string {
  return (
    `Silences ${countOf(count, 'incident', 'incidents')} at once, ` +
    `${muteDurationClause(mutedUntilIso)}. Each one keeps its own suppression ` +
    'key — its scan, its rule, its scope, the kind of signal and the direction ' +
    '— so nothing beyond the ones you selected is silenced: the same scopes ' +
    'moving the other way, a different kind of signal on them, another scan or ' +
    'another rule all still alert.'
  )
}

/**
 * What a batch just did, said out loud — one toast for N incidents.
 *
 * NOT `inboxActionSuccessMessage` with a plural patched in, and the difference
 * is behavioural rather than grammatical:
 *
 *  - It takes a COUNT and no response. The batch response's `groups` can be
 *    shorter than the request when an incident's deliveries were deleted
 *    concurrently, and that incident WAS still acted on and audited. Announcing
 *    `groups.length` would under-report a decision that fully landed, so the
 *    caller passes how many it asked for.
 *  - It must never read `overrides_written`. On this route the key is always
 *    sent and always null, meaning "not applicable" — `false_positive` is the
 *    only action that ratchets anything and the route refuses it. The single
 *    version's false-positive branch reads that number, which is precisely why
 *    reusing it here would be wrong even if `AlertInboxBulkAction` allowed the
 *    action to arrive.
 *  - The per-incident promises stay off it. "It reopens once the scope goes
 *    quiet" is true of each member individually and reads, over a batch, as a
 *    promise about the batch — which is not a thing that exists: there is no
 *    group object, so there is nothing to reopen as a unit.
 *
 * The mute branch still distinguishes a timed silence from an open-ended one,
 * because that distinction is the whole point and a bulk mute is the
 * case that reaches furthest.
 */
export function bulkInboxActionSuccessMessage(
  action: AlertInboxBulkAction,
  count: number,
  mutedUntilIso: string | null,
): string {
  const incidents = countOf(count, 'incident', 'incidents')
  switch (action) {
    case 'acknowledge':
      return `Acknowledged ${incidents}.`
    case 'resolve':
      return `Resolved ${incidents}.`
    case 'mute':
      return mutedUntilIso
        ? `Muted ${incidents} until ${formatDateTime(mutedUntilIso)}.`
        : `Muted ${incidents} — no end date. They stay quiet until you unmute them.`
    case 'reopen':
      // Both words, because one slot does both jobs: `reopen` clears an
      // acknowledge, a resolve and a false positive AND lifts a mute, and a
      // mixed selection is the normal case for a bulk reopen — there is no
      // single previous status to key the wording off, the way the
      // single-incident message does.
      return `Reopened ${incidents} — alerts resume.`
    case 'note':
      return `Note saved on ${incidents}.`
  }
}

/**
 * Pydantic prepends "Value error, " to every message a `model_validator` raises,
 * and it must not reach a toast.
 *
 * The prefix is an artefact of how the server DECLARED the rule, not part of
 * what it is telling the operator, and the one message a bulk caller can
 * realistically provoke is the long false-positive refusal — which ends by
 * naming the way to do it anyway, one incident at a time. Prefixing that with
 * "Value error," makes a deliberate, well-argued policy read like the page
 * broke.
 *
 * Applied to an already-flattened message string rather than to the raw error,
 * so this stays pure and testable and `lib/` gains no dependency on the API
 * client: `api/client.ts` already turns a FastAPI 422 `detail` array into one
 * string (dropping the `body` path segment), and this is the last step of that
 * same flattening.
 */
export function stripValueErrorPrefix(message: string): string {
  return message.startsWith('Value error, ') ? message.slice('Value error, '.length) : message
}
