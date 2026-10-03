import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { plannedEventsApi } from '@/api/plannedEvents'
import { Chip } from '@/components/primitives/chip'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { plannedEventExpectation } from '@/lib/plannedEvents'
import {
  activeSignalsKey,
  plannedWindowSuggestionsKey,
  projectMonitoringSeriesKey,
  projectPlannedEventsKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { PlannedWindowSuggestion } from '@/types'

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

function slotLabel(s: PlannedWindowSuggestion): string {
  return `Every ${WEEKDAYS[s.weekday] ?? '?'} at ${String(s.hour).padStart(2, '0')}:00 UTC`
}

function suggestionKey(s: PlannedWindowSuggestion): string {
  return `${s.scope_type}:${s.scope_ref}:${s.weekday}:${s.hour}`
}

/**
 * Recurring windows the project's *expected* verdicts point at (#271): the
 * same series keeps moving at the same hour of the same weekday, and people
 * keep saying so. Planning one creates its next windows as ordinary planned
 * events, after which it drops out of the list. Editors only; renders nothing
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
          description: `Suggested from ${s.verdict_count} expected verdicts on ${name}, ${slotLabel(s).toLowerCase()}.`,
          starts_at: window.starts_at,
          ends_at: window.ends_at,
          direction: s.direction,
          scope_type: s.scope_type,
          scope_ref: s.scope_ref,
        })
      }
      return s.windows.length
    },
    onSuccess: count => toast.success(`Planned the next ${count} windows`),
    onError: error => toast.error(`Could not plan the windows — ${getErrorMessage(error)}`),
    // Partly planned is still planned: refresh either way.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: projectPlannedEventsKey(slug) })
      void qc.invalidateQueries({ queryKey: projectMonitoringSeriesKey(slug) })
      void qc.invalidateQueries({ queryKey: activeSignalsKey(slug) })
    },
  })

  const suggestions = query.data ?? []
  if (suggestions.length === 0) return null

  return (
    <Panel
      title="Suggested recurring windows"
      subtitle="Signals at the same hour of the same weekday keep being marked expected. Plan the next ones, and they will be drawn without raising alerts."
    >
      <ul className="divide-y divide-border px-4 text-body-sm" data-testid="planned-window-suggestions">
        {suggestions.map(s => (
          <li key={suggestionKey(s)} className="flex flex-wrap items-center justify-between gap-2 py-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="font-medium">{s.scope_name ?? s.scope_ref}</span>
              <span className="text-fg-tertiary">{slotLabel(s)}</span>
              <Chip variant="outline" size="xs">{plannedEventExpectation(s.direction)}</Chip>
              <span className="text-fg-tertiary">
                {s.verdict_count} expected verdicts{s.note ? ` · “${s.note}”` : ''}
              </span>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={plan.isPending}
              onClick={() => plan.mutate(s)}
              aria-label={`Plan the next ${s.windows.length} windows on ${s.scope_name ?? s.scope_ref}`}
            >
              Plan next {s.windows.length}
            </Button>
          </li>
        ))}
      </ul>
    </Panel>
  )
}
