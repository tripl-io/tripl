import type { ChipTone } from '@/components/primitives/chip-variants'
import { isDriftOnly } from '@/lib/alertStatus'
import { formatSignalEffect } from '@/lib/monitoring'
import { hasBaseline } from '@/lib/percentDelta'
import { countOf } from '@/lib/plural'
import { signalDirectionTone } from '@/lib/statusLexicon'
import type { AlertInboxGroup } from '@/types'

/**
 * The most scope names one card carries: the server cuts `scope_names` here
 * (`INBOX_SCOPE_NAME_LIMIT`, backend services/_alerting_deliveries.py). A full
 * list can therefore stand for more scopes than it holds, and the card says
 * "8+" rather than a count it does not have.
 */
export const INBOX_SCOPE_NAME_LIMIT = 8

/**
 * The card's headline: the scope it is about, plus how many others it covers.
 *
 * The card used to lead with chips and put WHAT broke on its second line as a
 * muted comma list, so triage meant reading every card in full. The server
 * puts the scope of the newest, loudest firing first — the one the badge, the
 * counts and the scope link describe — and the rest loudest first, so the name
 * the card leads with is the one its numbers belong to.
 *
 * `capped` is true when the list is full, so "and 7 more" may be short.
 */
export function incidentHeadline(group: Pick<AlertInboxGroup, 'scope_names'>): {
  primary: string
  more: number
  capped: boolean
} {
  const [primary = '', ...rest] = group.scope_names
  return { primary, more: rest.length, capped: group.scope_names.length >= INBOX_SCOPE_NAME_LIMIT }
}

/** "and 3 more", or "and 7+ more" when the server's list was cut. */
export function incidentMoreScopesLabel(headline: { more: number; capped: boolean }): string {
  return `and ${headline.more}${headline.capped ? '+' : ''} more`
}

/**
 * "8 items across 4 scopes" — how often the incident was alerted, and how wide.
 *
 * An item is one scope's firing as one delivery carried it: repeat firings and
 * the copy each further delivery carries all count, which is why the number can
 * be larger than the signals Anomalies lists. Just the items for an incident on
 * one scope, the case live dispatch produces.
 */
export function incidentItemsLabel(group: Pick<AlertInboxGroup, 'item_count' | 'scope_names'>): string {
  const items = countOf(group.item_count, 'item', 'items')
  const scopes = group.scope_names.length
  if (scopes <= 1) return items
  if (scopes >= INBOX_SCOPE_NAME_LIMIT) return `${items} across ${scopes}+ scopes`
  return `${items} across ${scopes} scopes`
}

/**
 * The signed size of the change, for the badge at the right of the card's
 * title row: "+82%", "−59%", "dropped to zero".
 *
 * Signed by DIRECTION, through the same {@link formatSignalEffect} every signal
 * list uses, and coloured by {@link signalDirectionTone} — spike red, drop
 * amber — so one incident reads one number in one colour on the Inbox and on
 * Anomalies. Null when there is no baseline to be a percentage of: the
 * magnitude line already says "no baseline" in words, and a badge printing a
 * percentage of zero would contradict it. A drop to zero keeps its badge —
 * it is the one no-percentage case with a meaning worth a glance.
 */
export function incidentDeltaBadge(
  group: Pick<AlertInboxGroup, 'direction' | 'actual_count' | 'expected_count'>
    & Partial<Pick<AlertInboxGroup, 'scope_types'>>,
): { label: string; tone: ChipTone } | null {
  // A drift has no signed size: its counts are what the scan compared.
  if (isDriftOnly(group.scope_types ?? [])) return null
  const tone: ChipTone = signalDirectionTone(group.direction)
  if (group.direction === 'drop' && group.actual_count === 0) {
    return { label: 'dropped to zero', tone }
  }
  if (!hasBaseline(group.expected_count)) return null
  return {
    label: formatSignalEffect({
      direction: group.direction,
      actual_count: group.actual_count,
      expected_count: group.expected_count,
      // Read only when there is no baseline, which is ruled out above.
      z_score: 0,
    }),
    tone,
  }
}
