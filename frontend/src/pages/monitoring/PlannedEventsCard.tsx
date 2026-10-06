import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CalendarRange, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { plannedEventsApi } from '@/api/plannedEvents'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { INPUT_TEXT_CLASS } from '@/components/settings/input-style'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { DateTimePicker } from '@/components/ui/date-time-picker'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useConfirm } from '@/hooks/useConfirm'
import { formatUtcOffset, toDatetimeLocalValue } from '@/lib/chartAnnotations'
import { formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { MonitoringScope } from '@/lib/monitoring'
import {
  PLANNED_EVENT_LABEL_MAX,
  isValidPlannedWindow,
  plannedEventExpectation,
} from '@/lib/plannedEvents'
import {
  activeSignalsKey,
  projectKey,
  projectMonitoringSeriesKey,
  projectPlannedEventsKey,
  projectsKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { PlannedEvent } from '@/types'
import type { usePlannedEvents } from './usePlannedEvents'
import { useNow } from '@/hooks/useNow'

type DirectionChoice = 'either' | 'spike' | 'drop'

const DIRECTION_OPTIONS: { value: DirectionChoice; label: string }[] = [
  { value: 'spike', label: 'Expect a rise' },
  { value: 'drop', label: 'Expect a drop' },
  { value: 'either', label: 'Expect either' },
]

const DAY_MS = 24 * 60 * 60 * 1000

/**
 * Planned events (F18): a campaign, a sale or a holiday the reader expects to
 * move this series. Anomalies inside a window are still drawn, muted, but raise
 * no alert, notification or open signal. Created here for the chart in view;
 * the list also shows project-wide events, which cover every chart.
 */
export function PlannedEventsCard({
  slug,
  scope,
  scopeId,
  canWrite,
  query,
}: {
  slug: string
  scope: MonitoringScope
  scopeId: string
  canWrite: boolean
  query: ReturnType<typeof usePlannedEvents>
}) {
  const queryClient = useQueryClient()
  const events = query.data ?? []
  // Creating or deleting a window retags anomalies on the server, so every
  // surface that reads them refreshes: the series dots, the signals and the
  // sidebar badge.
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: projectPlannedEventsKey(slug) })
    void queryClient.invalidateQueries({ queryKey: projectMonitoringSeriesKey(slug) })
    void queryClient.invalidateQueries({ queryKey: activeSignalsKey(slug) })
    void queryClient.invalidateQueries({ queryKey: projectKey(slug) })
    void queryClient.invalidateQueries({ queryKey: projectsKey() })
  }

  // A day from now by default: the usual entry is "the promo runs today".
  const [startsAt, setStartsAt] = useState(() => toDatetimeLocalValue(new Date()))
  const [endsAt, setEndsAt] = useState(() => toDatetimeLocalValue(new Date(Date.now() + DAY_MS)))
  const [label, setLabel] = useState('')
  const [direction, setDirection] = useState<DirectionChoice>('spike')
  const windowValid = isValidPlannedWindow(startsAt, endsAt)

  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      plannedEventsApi.create(slug, {
        label: label.trim(),
        starts_at: new Date(startsAt).toISOString(),
        ends_at: new Date(endsAt).toISOString(),
        direction: direction === 'either' ? null : direction,
        scope_type: scope,
        scope_ref: scopeId,
      }),
    onSuccess: () => {
      setLabel('')
      invalidate()
      toast.success('Planned event added', {
        description: 'Anomalies inside the window are shown but not alerted on.',
      })
    },
  })

  const { confirm, dialog } = useConfirm()
  const deleteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => plannedEventsApi.delete(slug, id),
    onSuccess: invalidate,
    onError: error => toast.error(`Could not delete the planned event — ${getErrorMessage(error)}`),
  })
  const deleteEvent = async (event: PlannedEvent) => {
    const projectWide = event.scope_type === null
    const ok = await confirm({
      title: 'Delete planned event?',
      message: projectWide
        ? `"${event.label}" covers every chart in this project. Anomalies inside it will raise signals and alerts again.`
        : `Anomalies inside "${event.label}" will raise signals and alerts again.`,
      variant: 'danger',
      confirmLabel: 'Delete',
    })
    if (ok) deleteMut.mutate(event.id)
  }

  // Re-read each minute, so a page left open across a DST change says so.
  const now = useNow(60_000)
  const offset = formatUtcOffset(new Date(now))
  const hasBody = canWrite || createMut.isError || query.isError || events.length > 0
  const canSubmit = windowValid && label.trim().length > 0 && !createMut.isPending

  return (
    <Card id="planned-events" className="scroll-mt-4">
      <CardHeader>
        <div className="flex items-center gap-2">
          <CalendarRange aria-hidden="true" className="size-4 text-fg-tertiary" />
          <CardTitle as="h2">Planned events</CardTitle>
          <span className="tnum text-caption text-fg-tertiary">({events.length})</span>
        </div>
        <CardDescription>
          A campaign, sale or holiday you expect to move this series. Anomalies
          inside the window stay on the chart but raise no alert or signal.
          {!canWrite && ' Planning them is up to an editor or owner.'}
        </CardDescription>
      </CardHeader>
      {hasBody && (
        <CardContent className="space-y-3">
          {canWrite && (
            <form
              className="flex flex-wrap items-start gap-2"
              onSubmit={event => {
                event.preventDefault()
                if (!canSubmit) return
                createMut.mutate()
              }}
            >
              <div className="flex flex-col gap-0.5">
                <DateTimePicker
                  id="planned-event-start"
                  label="Starts"
                  value={startsAt}
                  onChange={setStartsAt}
                  aria-describedby="planned-event-time-hint"
                />
                <span id="planned-event-time-hint" className="text-micro text-fg-tertiary">
                  Local time ({offset})
                </span>
              </div>
              <div className="flex flex-col gap-0.5">
                <DateTimePicker
                  id="planned-event-end"
                  label="Ends"
                  value={endsAt}
                  onChange={setEndsAt}
                  aria-describedby="planned-event-time-hint"
                />
                {!windowValid && (
                  <span role="alert" className="text-micro text-destructive">
                    Ends after it starts
                  </span>
                )}
              </div>
              <div className="flex flex-col gap-0.5">
                <Label htmlFor="planned-event-label" className="sr-only">Planned event label</Label>
                <Input
                  id="planned-event-label"
                  placeholder="Label (e.g. Black Friday sale)"
                  value={label}
                  maxLength={PLANNED_EVENT_LABEL_MAX}
                  onChange={event => setLabel(event.target.value)}
                  className={`w-[240px] max-w-full ${INPUT_TEXT_CLASS}`}
                />
              </div>
              <Select value={direction} onValueChange={(value: DirectionChoice) => setDirection(value)}>
                <SelectTrigger className="w-[150px]" aria-label="Expected direction">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DIRECTION_OPTIONS.map(option => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button type="submit" variant="outline" disabled={!canSubmit} aria-label="Add planned event">
                Add
              </Button>
            </form>
          )}
          {createMut.isError && (
            <p role="alert" className="text-body-sm text-destructive">
              {createMut.error instanceof Error ? createMut.error.message : 'Failed to add the planned event.'}
            </p>
          )}
          {query.isError ? (
            <ErrorState
              compact
              title="Could not load planned events"
              error={query.error}
              onRetry={() => void query.refetch()}
            />
          ) : events.length > 0 && (
            <ul className="divide-y divide-border text-body-sm">
              {events.map(event => (
                <li key={event.id} className="flex items-center justify-between gap-2 py-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-fg-tertiary">
                      {formatTimestamp(event.starts_at)} – {formatTimestamp(event.ends_at)}
                    </span>
                    <span className="min-w-0 break-words font-medium">{event.label}</span>
                    <Chip variant="outline" size="xs">{plannedEventExpectation(event.direction)}</Chip>
                    {event.scope_type === null && (
                      <Chip variant="outline" size="xs">project-wide</Chip>
                    )}
                    {event.source === 'holiday' && <Chip variant="outline" size="xs">Holiday</Chip>}
                  </div>
                  {/* The holiday calendar owns its rows: changed in Detection settings. */}
                  {canWrite && event.source !== 'holiday' && (
                    <IconButton
                      variant="ghost"
                      className="h-7 w-7 shrink-0 text-fg-tertiary hover:text-destructive"
                      onClick={() => void deleteEvent(event)}
                      disabled={deleteMut.isPending && deleteMut.variables === event.id}
                      label={`Delete planned event ${event.label}`}
                    >
                      <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                    </IconButton>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      )}
      {dialog}
    </Card>
  )
}
