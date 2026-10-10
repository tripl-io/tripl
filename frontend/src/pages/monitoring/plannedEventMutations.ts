import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { plannedEventsApi } from '@/api/plannedEvents'
import type { ConfirmOptions } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  activeSignalsKey,
  projectKey,
  projectMonitoringSeriesKey,
  projectPlannedEventsKey,
  projectsKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { PlannedEvent } from '@/types'

/**
 * Creating or deleting an expected window (a `planned_event` in the API)
 * retags anomalies on the server, so everything that reads them refreshes:
 * every window list and the suggestions (one key root), the series dots, the
 * signals, and the project summary behind the sidebar badge and the
 * Overview's Open signals. One set for every surface that writes windows: the
 * Annotations page refreshed only the first three, and its deletes left the
 * badge counting signals that were gone.
 */
export function invalidatePlannedEventEffects(qc: QueryClient, slug: string | undefined): void {
  void qc.invalidateQueries({ queryKey: projectPlannedEventsKey(slug) })
  void qc.invalidateQueries({ queryKey: projectMonitoringSeriesKey(slug) })
  void qc.invalidateQueries({ queryKey: activeSignalsKey(slug) })
  void qc.invalidateQueries({ queryKey: projectKey(slug) })
  void qc.invalidateQueries({ queryKey: projectsKey() })
}

/**
 * The confirm before deleting a window, the same from a chart's card and from
 * the Annotations page. A project-wide window says so: deleting it from one
 * chart takes it off every chart.
 */
export function plannedEventDeleteConfirm(
  event: Pick<PlannedEvent, 'label' | 'scope_type'>,
): ConfirmOptions {
  const projectWide = event.scope_type === null
  return {
    title: projectWide ? 'Delete project-wide expected window?' : 'Delete expected window?',
    message: projectWide
      ? `"${event.label}" covers every chart in this project. Anomalies inside it will raise signals and alerts again, on every chart.`
      : `Anomalies inside "${event.label}" will raise signals and alerts again.`,
    variant: 'danger',
    confirmLabel: 'Delete',
  }
}

/** Deletes an expected window by id and refreshes what it retagged. */
export function usePlannedEventDelete(slug: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    // Its failure is toasted below, with what failed named.
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => plannedEventsApi.delete(slug!, id),
    onSuccess: () => invalidatePlannedEventEffects(qc, slug),
    onError: error => toast.error(`Could not delete the expected window — ${getErrorMessage(error)}`),
  })
}
