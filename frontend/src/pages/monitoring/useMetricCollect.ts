import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import {
  startMetricCollectionWatch,
  useIsMetricCollectionWatched,
} from '@/hooks/useMetricCollectionWatcher'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  metricDefinitionKey,
  metricsCatalogKey,
  monitoringSeriesKey,
  monitoringSeriesScopeKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'

/**
 * Everything a manual collect needs, captured when the button is pressed and
 * carried through the mutation and the watch. Nothing downstream re-reads the
 * route, so navigating mid-run cannot repoint the run at another metric or
 * another project.
 */
export type CollectTarget = {
  slug: string
  scope: string
  scopeId: string
  displayName: string
  isFactMetric: boolean
}

export interface MetricCollect {
  start: (target: CollectTarget) => void
  /** A collect of THIS page's metric is in flight or being watched. */
  isCollecting: boolean
}

/**
 * A fact click collects every metric sharing its source, in one batch — say how
 * many, rather than "all active dependent metrics": the batch is capped, so
 * that phrasing could promise more than the click actually started.
 */
function factCollectMessage(metricCount: number): string {
  return metricCount > 1
    ? `Source refresh started — ${metricCount} metrics sharing this source will update in one batch.`
    : 'Source refresh started — current fact data will update shortly.'
}

/**
 * Manual "collect now": backfill a recent window for a metric so its chart
 * populates without waiting for the scheduler. Collection runs in the worker;
 * a detached watch polls the persisted last_collection_status until the run
 * settles, toasts success or the persisted failure reason, and
 * refreshes the definition, series and catalog on either outcome.
 *
 * The watch is detached (owned by the watcher module, as in the catalog), so
 * leaving the page mid-run no longer drops the "you will be notified" promise:
 * the toast and the refresh still happen, and coming back to the metric shows
 * the spinner again while the run is going.
 */
export function useMetricCollect(scopeId: string): MetricCollect {
  const queryClient = useQueryClient()
  const { slug = '' } = useParams<{ slug: string }>()
  const { notifyMetricCollectStarted } = useDemoScenarioActions()
  const isWatched = useIsMetricCollectionWatched(slug, scopeId)

  // Refresh what the run just changed — keyed to the target captured at
  // collect-start, never whatever the page has since navigated to.
  // Runs on success AND error: a failed run still
  // rewrites the definition's status and the catalog row.
  const refreshAfterRun = (target: CollectTarget) => {
    void queryClient.invalidateQueries({
      queryKey: monitoringSeriesKey(target.slug, target.scope, target.scopeId),
    })
    void queryClient.invalidateQueries({ queryKey: metricDefinitionKey(target.slug, target.scopeId) })
    void queryClient.invalidateQueries({ queryKey: metricsCatalogKey(target.slug) })
    if (target.isFactMetric) {
      // A fact collect refreshes every active dependent metric in the shared
      // source batch, so every dependent series may change.
      void queryClient.invalidateQueries({
        queryKey: monitoringSeriesScopeKey(target.slug, 'metric'),
      })
    }
  }

  const collectMut = useMutation({
    meta: SILENT_ERROR_META,
    // The target travels WITH the mutation instead of being re-read in onSuccess.
    // react-query refreshes the observer's options every render, so onSuccess saw
    // the CURRENT scopeId: firing a collect for metric A and navigating to B
    // before the POST resolved attached the watcher to B.
    mutationFn: (target: CollectTarget) => metricsCatalogApi.collect(target.slug, target.scopeId),
    onSuccess: (data, target) => {
      toast.success(
        target.isFactMetric
          ? factCollectMessage(data.metric_count)
          : 'Collection started — you will be notified when it finishes.',
      )
      startMetricCollectionWatch(
        { slug: target.slug, metricId: target.scopeId, displayName: target.displayName },
        { onSettled: () => refreshAfterRun(target) },
      )
      // The scenario binds to the metric the USER collected — the demo's tick
      // runs collections of its own, so only this path counts.
      // Inert outside a ready demo project.
      notifyMetricCollectStarted(target.scopeId)
    },
    // Its own toast (silenced in the backstop), so the one message carries both
    // what failed and why.
    onError: error => toast.error(`Could not start collection — ${getErrorMessage(error)}`),
  })

  return {
    start: target => collectMut.mutate(target),
    // Key the spinner to the metric actually being collected — both while the
    // POST is in flight and while the watch polls — so a run on metric A does
    // not read as "collecting" once the page navigates to metric B.
    isCollecting:
      (collectMut.isPending && collectMut.variables?.scopeId === scopeId)
      || isWatched,
  }
}
