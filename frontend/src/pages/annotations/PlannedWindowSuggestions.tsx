import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { plannedEventsApi } from '@/api/plannedEvents'
import { Chip } from '@/components/primitives/chip'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { formatTimeOfDay, formatUtcOffset } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { APP_LOCALE } from '@/lib/format'
import { plannedEventExpectation } from '@/lib/plannedEvents'
import { countOf } from '@/lib/plural'
import { plannedWindowSuggestionsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { PlannedWindowSuggestion } from '@/types'
import { invalidatePlannedEventEffects } from '../monitoring/plannedEventMutations'

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

/** The slot as the server keys it, a weekday and an hour in UTC: "Monday at 09:00 UTC". */
function utcSlot(s: PlannedWindowSuggestion): string {
  return `${WEEKDAYS[s.weekday] ?? '?'} at ${String(s.hour).padStart(2, '0')}:00 UTC`
}

/**
 * The slot in the reader's own clock, as the expected windows listed under it
 * read: "Every Monday at 11:00 AM (UTC+2)". A UTC "09:00" above windows that
 * say "11:00 AM" left the reader to work out that they were the same slot.
 * Taken from the first window the server proposes, which starts on the slot;
 * without one, the slot as the server keys it.
 */
function slotLabel(s: PlannedWindowSuggestion): string {
  const first = s.windows[0] ? new Date(s.windows[0].starts_at) : null
  if (!first || Number.isNaN(first.getTime())) return `Every ${utcSlot(s)}`
  const weekday = first.toLocaleDateString(APP_LOCALE, { weekday: 'long' })
  return `Every ${weekday} at ${formatTimeOfDay(first)} (${formatUtcOffset(first)})`
}

function suggestionKey(s: PlannedWindowSuggestion): string {
  return `${s.scope_type}:${s.scope_ref}:${s.weekday}:${s.hour}`
}

/**
 * Recurring windows the project's *expected* verdicts point at (#271): the
 * same series keeps moving at the same hour of the same weekday, and people
 * keep saying so. Planning one creates its next windows as ordinary expected
 * windows, after which it drops out of the list. Editors only; renders nothing
 * when there is nothing to suggest.
 */
export function PlannedWindowSuggestions({ slug }: { slug: string }) {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: plannedWindowSuggestionsKey(slug),
    queryFn: () => plannedEventsApi.suggestions(slug),
    // A suggestion is a nicety: a failed load leaves the page as it was.
    meta: SILENT_ERROR_META,
  })

  const plan = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: async (s: PlannedWindowSuggestion) => {
      const name = s.scope_name ?? 'this series'
      for (const window of s.windows) {
        await plannedEventsApi.create(slug, {
          label: s.note ?? `Weekly window on ${name}`,
          // Stored text, read later in any zone, so it names the slot as the
          // server keys it (UTC) rather than in this reader's clock.
          description: `Suggested from ${countOf(s.verdict_count, 'expected verdict', 'expected verdicts')} on ${name}, every ${utcSlot(s)}.`,
          starts_at: window.starts_at,
          ends_at: window.ends_at,
          direction: s.direction,
          scope_type: s.scope_type,
          scope_ref: s.scope_ref,
        })
      }
      return s.windows.length
    },
    onSuccess: count => toast.success(`Planned the next ${countOf(count, 'window', 'windows')}`),
    onError: error => toast.error(`Could not plan the windows — ${getErrorMessage(error)}`),
    // Partly planned is still planned: refresh either way. The suggestions
    // sit under the same key root, so this drops the planned one too.
    onSettled: () => invalidatePlannedEventEffects(qc, slug),
  })

  const suggestions = query.data ?? []
  if (suggestions.length === 0) return null

  return (
    <Panel
      title="Suggested recurring windows"
      subtitle="Signals at the same hour of the same weekday keep being marked expected. Plan the next ones as expected windows, and anomalies in them never become a signal or an alert."
    >
      <ul className="divide-y divide-border px-4 text-body-sm" data-testid="planned-window-suggestions">
        {suggestions.map(s => (
          <li key={suggestionKey(s)} className="flex flex-wrap items-center justify-between gap-2 py-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="font-medium">{s.scope_name ?? s.scope_ref}</span>
              <span className="text-fg-tertiary">{slotLabel(s)}</span>
              <Chip variant="outline" size="xs">{plannedEventExpectation(s.direction)}</Chip>
              <span className="text-fg-tertiary">
                {countOf(s.verdict_count, 'expected verdict', 'expected verdicts')}{s.note ? ` · “${s.note}”` : ''}
              </span>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={plan.isPending}
              onClick={() => plan.mutate(s)}
              aria-label={`Plan the next ${countOf(s.windows.length, 'window', 'windows')} on ${s.scope_name ?? s.scope_ref}`}
            >
              Plan next {s.windows.length}
            </Button>
          </li>
        ))}
      </ul>
    </Panel>
  )
}
