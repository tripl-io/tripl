import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CalendarRange } from 'lucide-react'
import { toast } from 'sonner'
import { plannedEventsApi } from '@/api/plannedEvents'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { INPUT_TEXT_CLASS } from '@/components/settings/input-style'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { DateTimePicker } from '@/components/ui/date-time-picker'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useConfirm } from '@/hooks/useConfirm'
import { formatUtcOffset, toLocalDateTimeValue } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { MonitoringScope } from '@/lib/monitoring'
import { PLANNED_EVENT_LABEL_MAX, isValidPlannedWindow } from '@/lib/plannedEvents'
import type { PlannedEvent } from '@/types'
import { ExpectedWindowItem } from './ExpectedWindowItem'
import {
  invalidatePlannedEventEffects,
  plannedEventDeleteConfirm,
  usePlannedEventDelete,
} from './plannedEventMutations'
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
 * Expected windows (F18; a `planned_event` in the API): a campaign, a sale or
 * a holiday the reader expects to move this series. Anomalies inside a window
 * are still drawn, muted, but raise no alert, notification or open signal.
 * Created here for the chart in view; the list also shows project-wide
 * windows, which cover every chart.
 *
 * Not "planned events": in a tracking-plan product that already means the
 * events of the plan, as Coverage and Reconciliation use it.
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

  // A day from now by default: the usual entry is "the promo runs today".
  const [startsAt, setStartsAt] = useState(() => toLocalDateTimeValue(new Date()))
  const [endsAt, setEndsAt] = useState(() => toLocalDateTimeValue(new Date(Date.now() + DAY_MS)))
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
      invalidatePlannedEventEffects(queryClient, slug)
      toast.success('Expected window added', {
        description: 'Anomalies inside it are drawn muted and never become a signal or an alert.',
      })
    },
  })

  const { confirm, dialog } = useConfirm()
  const deleteMut = usePlannedEventDelete(slug)
  const deleteEvent = async (event: PlannedEvent) => {
    if (await confirm(plannedEventDeleteConfirm(event))) deleteMut.mutate(event.id)
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
          <CardTitle as="h2">Expected windows</CardTitle>
          <span className="tnum text-caption text-fg-tertiary">({events.length})</span>
        </div>
        <CardDescription>
          A campaign, sale or holiday you expect to move this series. Anomalies
          inside the window stay on the chart, muted, but never become a signal
          or an alert.
          {!canWrite && ' Planning them is up to an editor or owner.'}
        </CardDescription>
      </CardHeader>
      {hasBody && (
        <CardContent className="space-y-3">
          {canWrite && (
            <div className="space-y-1">
              <form
                className="flex flex-wrap items-end gap-2"
                onSubmit={event => {
                  event.preventDefault()
                  if (!canSubmit) return
                  createMut.mutate()
                }}
              >
                <div className="flex flex-col gap-0.5">
                  {/* Visible captions: the two pickers look alike otherwise. */}
                  <Label htmlFor="planned-event-start" className="text-micro text-fg-tertiary">Starts</Label>
                  <DateTimePicker
                    id="planned-event-start"
                    label="Starts"
                    value={startsAt}
                    onChange={setStartsAt}
                    aria-describedby="planned-event-time-hint"
                  />
                </div>
                <div className="flex flex-col gap-0.5">
                  <Label htmlFor="planned-event-end" className="text-micro text-fg-tertiary">Ends</Label>
                  <DateTimePicker
                    id="planned-event-end"
                    label="Ends"
                    value={endsAt}
                    onChange={setEndsAt}
                    aria-describedby="planned-event-time-hint"
                  />
                </div>
                <div className="flex flex-col gap-0.5">
                  <Label htmlFor="planned-event-label" className="sr-only">Expected window label</Label>
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
                <Button type="submit" variant="outline" disabled={!canSubmit} aria-label="Add expected window">
                  Add
                </Button>
              </form>
              {/* Under the row, not under one field, so the fields stay aligned. */}
              <p className="text-micro">
                <span id="planned-event-time-hint" className="text-fg-tertiary">
                  Local time ({offset})
                </span>
                {!windowValid && (
                  <span role="alert" className="ml-2 text-destructive">
                    Ends after it starts
                  </span>
                )}
              </p>
            </div>
          )}
          {createMut.isError && (
            <p role="alert" className="text-body-sm text-destructive">
              {createMut.error instanceof Error ? createMut.error.message : 'Failed to add the expected window.'}
            </p>
          )}
          {query.isError ? (
            <ErrorState
              compact
              title="Could not load expected windows"
              error={query.error}
              onRetry={() => void query.refetch()}
            />
          ) : events.length > 0 && (
            <ul className="divide-y divide-border text-body-sm">
              {events.map(event => (
                <ExpectedWindowItem
                  key={event.id}
                  event={event}
                  scope={event.scope_type === null && <Chip variant="outline" size="xs">project-wide</Chip>}
                  onDelete={canWrite ? () => void deleteEvent(event) : undefined}
                  deleting={deleteMut.isPending && deleteMut.variables === event.id}
                />
              ))}
            </ul>
          )}
        </CardContent>
      )}
      {dialog}
    </Card>
  )
}
