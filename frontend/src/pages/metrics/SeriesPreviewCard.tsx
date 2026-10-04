import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Loader2, Play } from 'lucide-react'
import { ErrorState } from '@/components/error-state'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { SCard } from '@/components/settings/kit'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { MetricPreviewResponse } from '@/types'
import { previewMetricSeries, type MetricSeriesPreviewRequest } from './catalogRequests'
import { MetricPreviewPanel } from './MetricPreviewPanel'
import type { MetricDraft } from './metricDraft'
import { seriesPreviewBlocker, seriesPreviewScope } from './seriesPreview'
import { useDebouncedRerun } from './useDebouncedRerun'

interface SeriesPreviewCardProps {
  slug: string
  draft: MetricDraft
  /** The definition a save would send; what the preview runs. */
  request: MetricSeriesPreviewRequest
  canWrite: boolean
}

/**
 * The series preview for fact and event-composition metrics: the chart,
 * range and last value SQL metrics already had, from a server dry run of the
 * definition this form would save. Only Preview starts it; once it has run, an
 * edit re-runs it a moment after the author stops typing.
 */
export function SeriesPreviewCard({ slug, draft, request, canWrite }: SeriesPreviewCardProps) {
  const requestKey = JSON.stringify(request)
  // The result, and the definition it describes: an edited draft hides it
  // rather than drawing the old definition's line beside the new one.
  const [shown, setShown] = useState<{ key: string; result: MetricPreviewResponse } | null>(null)
  const [armed, setArmed] = useState(false)
  // The definition last sent, whatever came back: a failed run is not retried
  // on a timer, only after the next edit or click.
  const [attemptedKey, setAttemptedKey] = useState<string | null>(null)
  const previewMut = useMutation({
    // Transport failures render inline below the button.
    meta: SILENT_ERROR_META,
    mutationFn: (body: MetricSeriesPreviewRequest) => previewMetricSeries(slug, body),
  })

  const blocker = seriesPreviewBlocker(draft)
  const current = shown?.key === requestKey ? shown.result : null

  const run = () => {
    const key = requestKey
    setAttemptedKey(key)
    // Per-call callback: a run the author has since edited past paints
    // nothing, because its key no longer matches.
    previewMut.mutate(request, { onSuccess: result => setShown({ key, result }) })
  }
  const onPreview = () => {
    setArmed(true)
    run()
  }
  useDebouncedRerun({
    armed,
    stale: blocker === null && attemptedKey !== requestKey && !previewMut.isPending,
    inputKey: requestKey,
    run,
  })

  return (
    <SCard title="Preview" description="What the metric's series would look like, before saving.">
      <div className="px-4 py-[15px]">
        <div className="flex flex-wrap items-center gap-[10px]">
          <button
            type="button"
            onClick={onPreview}
            disabled={!canWrite || blocker !== null || previewMut.isPending}
            {...disabledReasonAria('metric-series-preview', blocker)}
            className="inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-50 border-border text-fg-secondary"
          >
            {previewMut.isPending ? <Loader2 className="animate-spin" size={12} /> : <Play size={12} />}
            {previewMut.isPending ? 'Running…' : 'Preview'}
          </button>
          {blocker ? (
            <DisabledReason id="metric-series-preview" reason={blocker} />
          ) : (
            <span className="text-caption text-fg-tertiary">
              {armed && attemptedKey !== requestKey && !previewMut.isPending
                ? 'The definition changed; the preview runs again in a moment.'
                : seriesPreviewScope(draft)}
            </span>
          )}
        </div>
        {previewMut.isError && (
          <div className="mt-[10px]">
            <ErrorState compact title="Preview failed" error={previewMut.error} />
          </div>
        )}
        {current && (
          <MetricPreviewPanel result={current} color={draft.color} unit={draft.unit} variant="series" />
        )}
      </div>
    </SCard>
  )
}
