import type { ChipTone } from '@/components/primitives/chip-variants'
import { isDriftOnly } from '@/lib/alertStatus'
import { formatSignalEffect } from '@/lib/monitoring'
import { hasBaseline } from '@/lib/percentDelta'
import type { AlertInboxGroup } from '@/types'

/**
 * The card's headline: the first scope it names, plus how many more.
 *
 * The card used to lead with chips and put WHAT broke on its second line as a
 * muted comma list, so triage meant reading every card in full. The first name
 * is the one the correlation key sorted first; the rest stay reachable through
 * the `title` on the "+N more" beside it.
 */
export function incidentHeadline(group: Pick<AlertInboxGroup, 'scope_names'>): {
  primary: string
  more: number
} {
  const [primary = '', ...rest] = group.scope_names
  return { primary, more: rest.length }
}

/**
 * The signed size of the change, for the badge at the right of the card's
 * title row: "+82%", "−59%", "dropped to zero".
 *
 * Signed by DIRECTION, through the same {@link formatSignalEffect} every signal
 * list uses, so one incident reads one number on the Inbox and on Anomalies.
 * Null when there is no baseline to be a percentage of: the magnitude line
 * already says "no baseline" in words, and a badge printing a percentage of
 * zero would contradict it. A drop to zero keeps its badge —
 * it is the one no-percentage case with a meaning worth a glance.
 */
export function incidentDeltaBadge(
  group: Pick<AlertInboxGroup, 'direction' | 'actual_count' | 'expected_count'>
    & Partial<Pick<AlertInboxGroup, 'scope_types'>>,
): { label: string; tone: ChipTone } | null {
  // A drift has no signed size: its counts are what the scan compared.
  if (isDriftOnly(group.scope_types ?? [])) return null
  const tone: ChipTone = group.direction === 'drop' ? 'danger' : 'warning'
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
