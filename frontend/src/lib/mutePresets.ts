/**
 * Preset mute durations, and the sentences the mute buttons are announced by —
 * one definition for every surface that can silence something.
 *
 * Two surfaces mute and they disagreed. `MonitorDetailPage` offered 1h / 24h /
 * 7d on labelled buttons; the incident Inbox hardcoded
 * `Date.now() + 7 * 86_400_000` inside its mutation and put the word "Mute" on
 * an unlabelled button, so an operator could not find out how long they had
 * just snoozed something for — and re-clicking Mute silently extended it
 * another week.
 *
 * A preset is a DURATION, never a stored instant: the contract requires
 * `muted_until` to be in the future, so it is rendered into an absolute
 * timestamp at the moment of the click.
 *
 * There are deliberately TWO exported lists. {@link MUTE_PRESETS} is durations
 * only and is what every mute surface maps; {@link INBOX_MUTE_CHOICES} is that
 * list plus the open-ended {@link INDEFINITE_MUTE}, and only the incident Inbox
 * may map it — see the note on `INDEFINITE_MUTE` for why the same button on an
 * alert rule would UNMUTE it.
 *
 * The button NAMES live here for the same reason the durations and the
 * open-ended label do. Three surfaces — the incident Inbox, the Monitors row
 * and the monitor detail page — each re-typed `Mute <target> for <duration>`
 * and `Unmute <target>` into an `aria-label`, and each one's test file re-typed
 * them again to assert against. `MonitorDetailPage.test.tsx` even named the
 * missing piece in a comment: "the only real guard against the three surfaces
 * drifting would be a shared name builder next to `MUTE_PRESETS`". That builder
 * is {@link muteName} / {@link muteChoiceName} / {@link unmuteName} at the foot
 * of this file.
 *
 * The module owns the MUTE words ONLY. A surface's own vocabulary stays with
 * that surface: the Inbox's "Change mute on <target>" (no other mute surface
 * can change a mute in place — a muted rule offers a direct Unmute) and its
 * "Reopen <target>" (not a mute word at all; it lifts acknowledge, resolve and
 * false-positive too). Neither has a second surface to drift
 * from, so hosting them here would export one component's state vocabulary into
 * two components that can never use it.
 */

/** One offered duration. The label is the duration, so no mute is silent. */
export interface MutePreset {
  label: string
  ms: number
}

const HOUR_MS = 60 * 60 * 1000

export const MUTE_PRESETS: readonly MutePreset[] = [
  { label: '1h', ms: HOUR_MS },
  { label: '24h', ms: 24 * HOUR_MS },
  { label: '7d', ms: 7 * 24 * HOUR_MS },
]

/**
 * "Until I unmute" — a mute with no end at all, transmitted as a NULL
 * `muted_until`.
 *
 * A preset above is a DURATION; this is the deliberate ABSENCE of one, which is
 * why it is a separate type with `ms: null` rather than a fourth member of
 * `MUTE_PRESETS`. The split is load-bearing, not stylistic:
 *
 *  - On an INCIDENT (`alert_inbox`), `status = 'muted'` with a NULL
 *    `muted_until` is an indefinite mute: `_effective_inbox_status` only lapses
 *    a mute when `muted_until` is set AND has passed, so a NULL one never
 *    lapses and only Unmute lifts it.
 *  - On an ALERT RULE, `is_rule_muted()` returns FALSE the moment `muted_until`
 *    is NULL. The identical button on a rule surface would therefore UN-silence
 *    the rule it promised to silence forever. The permanent lever on a rule is
 *    its enable/disable switch, not a mute.
 *
 * Because `MUTE_PRESETS` is `readonly MutePreset[]` and `ms` there is `number`,
 * appending this to it is a compile error rather than a review comment — the
 * leak cannot be introduced by someone "simplifying" the two lists into one.
 *
 * The wording lives here, once, for the same reason the durations do: two
 * surfaces inventing "Forever" and "Indefinitely" for one idea is exactly the
 * vocabulary drift this module was written to stop.
 */
export interface IndefiniteMute {
  label: string
  /** The discriminant AND the wire value: no duration, so no end instant. */
  ms: null
}

/** Either kind of silence a surface can offer — a duration, or no end at all. */
export type MuteChoice = MutePreset | IndefiniteMute

export const INDEFINITE_MUTE: IndefiniteMute = { label: 'Until I unmute', ms: null }

/**
 * What the incident Inbox offers, and the ONLY list that carries the open-ended
 * choice.
 *
 * Named after its surface on purpose: the scope boundary is then a readable
 * line in the shared module instead of a `.filter(…)` incantation that each of
 * the rule surfaces has to remember to repeat (and that a fourth surface added
 * next year would forget). The timed presets stay first, so the riskiest choice
 * is never the leftmost, default-looking button.
 */
export const INBOX_MUTE_CHOICES: readonly MuteChoice[] = [...MUTE_PRESETS, INDEFINITE_MUTE]

/**
 * The absolute instant a duration resolves to, in the form the API accepts.
 *
 * `now` is injectable so a test can assert the boundary it expects rather than
 * racing the clock.
 */
export function muteUntilIso(durationMs: number, now: number = Date.now()): string {
  return new Date(now + durationMs).toISOString()
}

/**
 * The wire value for either kind of choice: an instant, or `null` for "no end".
 *
 * `null` and not `undefined`, and that distinction is the whole reason this
 * lives here rather than being re-derived at each call site: the request body
 * is assembled with an object spread, so an `undefined` would be OMITTED and
 * the most far-reaching mute on the page would post `{action: 'mute'}` with no
 * `muted_until` at all — a different request, meaning "unspecified" rather than
 * "deliberately open-ended".
 */
export function muteChoiceUntilIso(choice: MuteChoice, now: number = Date.now()): string | null {
  return choice.ms === null ? null : muteUntilIso(choice.ms, now)
}

/*
 * ---------------------------------------------------------------------------
 * The words. Above this line a choice becomes a WIRE VALUE; below it, the
 * sentence a screen reader announces. Same inputs, same discriminant, one
 * module — which is the whole point of this module: three surfaces were writing
 * that sentence themselves, in template literals no compiler or test could
 * compare with each other.
 *
 * Order below is the order an operator meets them: open the menu, pick a
 * duration, later lift the mute.
 * ---------------------------------------------------------------------------
 */

/**
 * The accessible name of the control that OPENS the duration menu.
 *
 * Used by the incident Inbox and by the Monitors row. Both are DISCLOSURE
 * toggles: they reveal the durations and mute nothing on their own. That is why
 * this takes no choice and never will — a button that COMMITS a mute must be
 * named by {@link muteChoiceName}, so the duration it is about to write is part
 * of its name. "Mute <target>" on a control that silently posts seven days is
 * the exact defect this module was extracted to end, so an
 * optional second parameter here would be an invitation to reintroduce it.
 *
 * `target` is a plain string and not a group or a monitor, because all three
 * call sites already hold one: the Inbox passes `scopeSummary(group)`, both
 * Monitors surfaces pass the rule name. This module must not learn what a scope
 * or a rule is.
 *
 * The Inbox's already-muted counterpart, "Change mute on <target>", is
 * deliberately NOT hosted here. It exists on one surface and cannot exist on
 * another — when a rule is muted both Monitors surfaces replace the control
 * with a direct Unmute, so there is no change-in-place affordance to name — and
 * therefore no second vocabulary for it to drift from. The Inbox reads
 * `isMuted ? `Change mute on ${target}` : muteName(target)`: an asymmetric
 * ternary on purpose, because one branch is shared vocabulary and the other is
 * that surface's own. Renaming this function is the one change that could
 * desynchronise those two branches, so grep for the ternary when you do.
 */
export function muteName(target: string): string {
  return `Mute ${target}`
}

/**
 * The accessible name of a button that COMMITS a mute — the one form that has
 * to say how long for.
 *
 * It covers both offered shapes and selects between them on `choice.ms === null`,
 * the same discriminant {@link muteChoiceUntilIso} branches on. The module now
 * decides twice from one fact — once for the wire value, once for the words —
 * which is a legible symmetry rather than a duplicated condition, and it is why
 * the parameter is a `MuteChoice` and never a label string: keying the phrasing
 * off wording would let a rule surface reach the open-ended sentence by passing
 * `INDEFINITE_MUTE.label`, and `is_rule_muted()` reads what that button writes
 * as NOT MUTED.
 *
 * THE ASYMMETRY, which is the strongest single reason this function exists. The
 * open-ended button's visible face is `INDEFINITE_MUTE.label` ("Until I
 * unmute") but its accessible name is "…until unmuted", because splicing the
 * label into the duration sentence yields "Mute <target> for Until I unmute",
 * which is not English. That knowledge used to live in a comment beside the one
 * call site that had the branch; a fourth mute surface would have had to
 * rediscover it.
 *
 * It is also a KNOWN, deliberate WCAG 2.5.3 (Label in Name) deviation, and the
 * only one in this module: for every {@link MUTE_PRESETS} entry the visible
 * label is a substring of the accessible name, so "click 1h" keeps working for
 * speech input — but "click Until I unmute" does not activate the open-ended
 * button, because that phrase is not in its name. `mutePresets.test.ts` asserts
 * the deviation rather than ignoring it, so it stays singular: a fifth form
 * must either satisfy the substring property or be added there, visibly, with a
 * reason.
 *
 * WHAT THIS DOES NOT WIDEN. Accepting a `MuteChoice` does not let a rule
 * surface offer the open-ended form. That guarantee lives exactly where it
 * always did and is untouched by this file's new section: `MUTE_PRESETS` is
 * `readonly MutePreset[]` whose `ms` is `number`, so a surface mapping it holds
 * only `MutePreset` values and its call here is statically confined to the
 * duration branch. Reaching the other branch still requires importing
 * `INDEFINITE_MUTE` or `INBOX_MUTE_CHOICES` by name — a visible, greppable,
 * reviewable act.
 *
 * ONE CANARY IS LOST, and is replaced deliberately. The two rule surfaces used
 * to write the duration sentence as a literal, so an open-ended choice reaching
 * them would have rendered the visibly broken "Mute X for Until I unmute". They
 * now call this, so the same leak would read as grammatical English and could
 * ship unnoticed. The replacement is behavioural and strictly stronger: both
 * rule surfaces assert that no button carrying the open-ended NAME is rendered,
 * and they build that name with this function so the guard cannot go stale the
 * day the phrasing is reworded (`MonitorsSection.test.tsx`,
 * `MonitorDetailPage.test.tsx`).
 */
export function muteChoiceName(target: string, choice: MuteChoice): string {
  return choice.ms === null
    ? `Mute ${target} until unmuted`
    : `Mute ${target} for ${choice.label}`
}

/**
 * The accessible name of the control that LIFTS a mute.
 *
 * The one form with no variant anywhere: all three surfaces already spelled it
 * "Unmute <target>", and `MonitorDetailPage.test.tsx` had independently reached
 * for a local helper of this very name before the export existed.
 *
 * THE TRAP, because this is where the next reader will be tempted. In the
 * incident Inbox this button is one half of a fixed slot whose other half reads
 * "Reopen <target>" — the file writes
 * `isMuted ? unmuteName(target) : `Reopen ${target}``. "Reopen" is NOT mute
 * vocabulary: it is the Inbox's own action for lifting acknowledge, resolve and
 * false-positive, and it does that second, different job on a card that was
 * never muted. Only the Unmute half comes from here. Do not grow
 * a `reopenName` in this module, and do not fold that ternary into a single
 * builder — a two-state accessible name is written as a ternary of two WHOLE
 * names, never as a ternary of verb fragments glued to a target, so that the
 * shared half and the surface's own half stay tellable apart.
 */
export function unmuteName(target: string): string {
  return `Unmute ${target}`
}
