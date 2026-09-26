import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CalendarPlus, ExternalLink, Rocket, Tag, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { INPUT_TEXT_CLASS } from '@/components/settings/input-style'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { DateTimePicker } from '@/components/ui/date-time-picker'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useConfirm } from '@/hooks/useConfirm'
import {
  ANNOTATION_DEFAULT_COLOR,
  ANNOTATION_LABEL_MAX,
  annotationMarkerColor,
  annotationSourceLabel,
  formatUtcOffset,
  isAutomaticAnnotation,
  safeAnnotationUrl,
  toDatetimeLocalValue,
} from '@/lib/chartAnnotations'
import { formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { MonitoringScope } from '@/lib/monitoring'
import { chartAnnotationsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { ChartAnnotation } from '@/types'
import type { useChartAnnotations } from './useChartAnnotations'

export function AnnotationsCard({
  slug,
  scope,
  scopeId,
  canWrite,
  query,
  prefillBucket = null,
  dataEnd = null,
}: {
  slug: string
  scope: MonitoringScope
  scopeId: string
  canWrite: boolean
  query: ReturnType<typeof useChartAnnotations>
  /**
   * A bucket to put in the form, e.g. the flagged one when the signal
   * banner's "Annotate" sent the reader here (JR-5).
   */
  prefillBucket?: string | null
  /** Where the collected series ends (the newest bucket's end), to say when a
   *  new annotation is past the data (MO-8). */
  dataEnd?: string | null
}) {
  const queryClient = useQueryClient()
  const annotations = query.data ?? []
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: chartAnnotationsKey(slug, scope, scopeId) })

  // Prefilled with "now": the usual annotation is "we just deployed".
  const [bucket, setBucket] = useState(() =>
    toDatetimeLocalValue(prefillBucket ? new Date(prefillBucket) : new Date()))
  const [label, setLabel] = useState('')
  // Adopt a new prefill during render, not in an effect, so the form never
  // paints the old time first.
  const [appliedPrefill, setAppliedPrefill] = useState(prefillBucket)
  if (prefillBucket && prefillBucket !== appliedPrefill) {
    setAppliedPrefill(prefillBucket)
    setBucket(toDatetimeLocalValue(new Date(prefillBucket)))
  }
  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      chartAnnotationsApi.create(slug, {
        bucket: new Date(bucket).toISOString(),
        label: label.trim(),
        // Never the backend's red default: red is the anomaly colour (MON-25).
        color: ANNOTATION_DEFAULT_COLOR,
        scope_type: scope,
        scope_ref: scopeId,
      }),
    onSuccess: created => {
      setBucket(toDatetimeLocalValue(new Date()))
      setLabel('')
      void invalidate()
      // The list grows below the fold and the marker may sit at the chart's
      // right edge, so say it worked (MO-8). The default "now" is usually past
      // the newest collected bucket: the chart then parks the marker on that
      // bucket, and the toast says why it is not where it was placed yet.
      const endTime = dataEnd ? new Date(dataEnd).getTime() : Number.NaN
      const createdTime = created?.bucket ? new Date(created.bucket).getTime() : Number.NaN
      if (!Number.isNaN(endTime) && createdTime > endTime) {
        toast.success('Annotation added', {
          description: 'Its time is past the collected data, so it shows on the newest bucket until the next collection.',
        })
      } else {
        toast.success('Annotation added')
      }
    },
  })

  const { confirm, dialog } = useConfirm()
  const deleteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => chartAnnotationsApi.delete(slug, id),
    onSuccess: () => {
      void invalidate()
    },
    onError: error => toast.error(`Could not delete the annotation — ${getErrorMessage(error)}`),
  })
  // One click used to delete at once, and the list includes project-wide
  // markers that every chart in the project draws (MON-26).
  const deleteAnnotation = async (annotation: ChartAnnotation) => {
    const projectWide = annotation.scope_type === null
    const ok = await confirm({
      title: projectWide ? 'Delete project-wide annotation?' : 'Delete annotation?',
      message: projectWide
        ? `"${annotation.label}" is shown on every chart in this project, not only this one. Deleting it removes it everywhere.`
        : `"${annotation.label}" will be removed from this chart.`,
      variant: 'danger',
      confirmLabel: 'Delete',
    })
    if (ok) deleteMut.mutate(annotation.id)
  }
  const offset = formatUtcOffset(new Date())
  // A viewer with nothing annotated has no body to show: the header says it
  // all, and an empty padded body would read as a missing list.
  const hasBody = canWrite || createMut.isError || query.isError || annotations.length > 0

  return (
    // The shared section-card geometry (DS-4 / MO-10): header bar with the
    // 12.5px h2 and its subtitle, then the body.
    <Card id="chart-annotations" className="scroll-mt-4">
      <CardHeader>
        <div className="flex items-center gap-2">
          <CalendarPlus aria-hidden="true" className="size-4 text-fg-tertiary" />
          <CardTitle as="h2">Annotations</CardTitle>
          <span className="tnum text-caption text-fg-tertiary">
            ({annotations.length})
          </span>
        </div>
        <CardDescription>
          Mark deploys, releases, or incidents so the chart shows what
          changed when. Snaps to the closest bucket of the current
          scope.
          {!canWrite && ' Adding and removing them is done by an editor or owner.'}
        </CardDescription>
      </CardHeader>
      {hasBody && (
        <CardContent className="space-y-3">
          {canWrite && (
            <form
              className="flex flex-wrap items-start gap-2"
              onSubmit={event => {
                event.preventDefault()
                if (!bucket || !label.trim()) return
                createMut.mutate()
              }}
            >
              <div className="flex flex-col gap-0.5">
                {/* The design-system picker, not the native datetime-local input
                    whose popup ignored the theme (MON-27, LIVE-21). Same value
                    format, so the ISO conversion below is unchanged. */}
                <DateTimePicker
                  id="annotation-bucket"
                  label="Date and time"
                  value={bucket}
                  onChange={setBucket}
                  aria-describedby="annotation-bucket-hint"
                />
                <span id="annotation-bucket-hint" className="text-micro text-fg-tertiary">
                  Your local time ({offset})
                </span>
              </div>
              <div className="flex flex-col gap-0.5">
                <Label htmlFor="annotation-label" className="sr-only">Label</Label>
                <Input
                  id="annotation-label"
                  placeholder="Label (e.g. v1.4 deploy)"
                  value={label}
                  maxLength={ANNOTATION_LABEL_MAX}
                  onChange={event => setLabel(event.target.value)}
                  // 16px on phones so iOS does not zoom in on focus (MT-27).
                  className={`w-[280px] max-w-full ${INPUT_TEXT_CLASS}`}
                />
              </div>
              <Button
                type="submit"
                variant="outline"
                disabled={!bucket || !label.trim() || createMut.isPending}
              >
                Add
              </Button>
            </form>
          )}
          {createMut.isError && (
            <p role="alert" className="text-body-sm text-destructive">
              {createMut.error instanceof Error ? createMut.error.message : 'Failed to add annotation.'}
            </p>
          )}
          {query.isError ? (
            <ErrorState
              compact
              title="Could not load annotations"
              error={query.error}
              onRetry={() => void query.refetch()}
            />
          ) : annotations.length > 0 && (
            <ul className="divide-y divide-border text-body-sm">
              {annotations.map(annotation => {
                const automatic = isAutomaticAnnotation(annotation)
                const url = safeAnnotationUrl(annotation.url)
                return (
                  <li
                    key={annotation.id}
                    className="flex items-center justify-between gap-2 py-2"
                  >
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span
                        aria-hidden="true"
                        className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                        style={{ backgroundColor: annotationMarkerColor(annotation) }}
                      />
                      <span className="text-fg-tertiary">
                        {formatTimestamp(annotation.bucket)}
                      </span>
                      <span className={`min-w-0 break-words font-medium${automatic ? ' text-fg-secondary' : ''}`}>
                        {annotation.label}
                      </span>
                      {/* Who made it: the metrics worker or a deploy script,
                          not someone in this form (#256). */}
                      {automatic && (
                        <Chip
                          variant="outline"
                          size="xs"
                          icon={annotation.source === 'release'
                            ? <Tag aria-hidden="true" />
                            : <Rocket aria-hidden="true" />}
                        >
                          {annotationSourceLabel(annotation.source)}
                        </Chip>
                      )}
                      {annotation.scope_type === null && (
                        <Chip variant="outline" size="xs">project-wide</Chip>
                      )}
                      {url && (
                        <a
                          href={url}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={`Details for ${annotation.label} (opens in a new tab)`}
                          className="inline-flex items-center gap-1 text-caption text-fg-tertiary hover:text-fg underline-offset-2 hover:underline"
                        >
                          Details
                          <ExternalLink aria-hidden="true" className="size-3" />
                        </a>
                      )}
                    </div>
                    {canWrite && (
                      <IconButton
                        variant="ghost"
                        className="h-7 w-7 shrink-0 text-fg-tertiary hover:text-destructive"
                        onClick={() => void deleteAnnotation(annotation)}
                        // Only the row being deleted waits, not every row.
                        disabled={deleteMut.isPending && deleteMut.variables === annotation.id}
                        label={`Delete annotation ${annotation.label}`}
                      >
                        <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                      </IconButton>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      )}
      {dialog}
    </Card>
  )
}
