import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { scansApi } from '@/api/scans'
import type { IntervalCode, ScanConfig } from '@/types'
import { Button } from '@/components/ui/button'
import { DateTimePicker } from '@/components/ui/date-time-picker'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { FieldError } from '@/components/forms/FieldError'
import { REQUIRED_MESSAGE, invalidAria } from '@/components/forms/validation'
import { toLocalDateTimeValue } from '@/lib/datetime'
import { getBucketStart, type MetricsGranularity } from '@/lib/metrics'
import { getErrorMessage } from '@/lib/utils'
import { scanJobsKey, scansKey } from '@/lib/queryKeys'

const DAY_MS = 24 * 60 * 60 * 1000

/**
 * How far past a bucket boundary this machine's clock must already be before the
 * seed will end the period on that boundary.
 *
 * The backend accepts `time_to <= floor_to_bucket(SERVER now, interval)` with a
 * strict comparison and no tolerance. The seed is computed from the BROWSER's
 * clock, so a browser running fast can floor onto a boundary the server has not
 * reached yet and the dialog's own untouched default is refused with a 400. That
 * happens exactly while `browserNow - boundary < skew`, so waiting out this much
 * of the bucket before using it is the whole guard.
 *
 * Two minutes is ordinary drift on a laptop that is not running NTP. The cost is
 * paid only inside that window and only there: for the first two minutes of a
 * bucket the default ends one bucket earlier, and the user can still type the
 * newer one in. (A hypothetical interval shorter than the margin would always
 * step back one bucket; the shortest the backend supports is `15m`.)
 */
export const CLOCK_SKEW_MARGIN_MS = 2 * 60 * 1000

/**
 * The chart granularity that bins on the same grid as each collection interval,
 * so the seeded period can be floored through `getBucketStart` — the frontend
 * half of the bucket contract in backend/src/tripl/core/bucketing.py — instead
 * of a second copy of the origins and widths here.
 */
const GRANULARITY_FOR_INTERVAL: Record<IntervalCode, MetricsGranularity> = {
  '15m': '15min',
  '1h': 'hour',
  '6h': '6h',
  '1d': 'day',
  '1w': 'week',
}

/**
 * A period the backend will accept: it ends on the last COMPLETE bucket of this
 * scan's own interval — or, for the first `CLOCK_SKEW_MARGIN_MS` of that bucket,
 * on the one before it.
 *
 * The seed used to be "the current local hour", which reaches into the interval
 * still filling for every config coarser than an hour — and for an hourly one in
 * a half-hour-offset timezone. Replay refuses such a period (it holds no
 * complete bucket), so the dialog's own defaults were rejected on any 6h/1d/1w
 * scan.
 */
function defaultReplayWindow(interval: IntervalCode | null): { from: string; to: string } {
  const granularity = GRANULARITY_FOR_INTERVAL[interval ?? '1h']
  const now = Date.now()
  const latest = new Date(getBucketStart(new Date(now).toISOString(), granularity))
  // The bucket width, read off the same grid rather than tabulated a second
  // time: flooring the instant just before `latest` lands on the previous boundary.
  const previous = new Date(
    getBucketStart(new Date(latest.getTime() - 1).toISOString(), granularity),
  )
  const width = latest.getTime() - previous.getTime()
  // Only claim the newest boundary once this clock is far enough past it that a
  // server clock trailing by up to CLOCK_SKEW_MARGIN_MS has crossed it too —
  // see that constant. Below the margin the previous boundary is the newest one
  // the backend is certain to accept.
  const to = now - latest.getTime() < CLOCK_SKEW_MARGIN_MS ? previous : latest
  const from = new Date(to.getTime() - Math.max(DAY_MS, width))
  return { from: toLocalDateTimeValue(from), to: toLocalDateTimeValue(to) }
}

// Replays metrics for a past time window. Opened from the scan page's header.
export function ReplayDialog({
  slug,
  scanConfig,
  open,
  onOpenChange,
}: {
  slug: string
  scanConfig: ScanConfig
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const qc = useQueryClient()
  // Seeded once, at least 24h wide and ending on the last complete bucket of
  // this scan's interval that the backend is certain to accept; the user can
  // adjust before replaying. Resolved in ONE call so both ends come off the same
  // instant — two calls either side of a bucket boundary would seed a period one
  // bucket wider than it looks.
  const [seed] = useState(() => defaultReplayWindow(scanConfig.interval))
  const [from, setFrom] = useState(seed.from)
  const [to, setTo] = useState(seed.to)
  // Inline "Required" after a submit with a cleared end, not a browser
  // bubble.
  const [submitted, setSubmitted] = useState(false)
  const fromError = submitted && !from ? REQUIRED_MESSAGE : null
  const toError = submitted && !to ? REQUIRED_MESSAGE : null

  const replayMut = useMutation({
    mutationFn: () => {
      if (!from || !to) throw new Error('Select a period to replay')
      const fromDate = new Date(from)
      const toDate = new Date(to)
      if (Number.isNaN(fromDate.getTime()) || Number.isNaN(toDate.getTime())) {
        throw new Error('Period dates are invalid')
      }
      if (fromDate >= toDate) throw new Error('Start must be before end')
      return scansApi.replayMetrics(slug, scanConfig.id, {
        time_from: fromDate.toISOString(),
        time_to: toDate.toISOString(),
      })
    },
    onSuccess: () => {
      onOpenChange(false)
      qc.invalidateQueries({ queryKey: scanJobsKey(slug, scanConfig.id) })
      qc.invalidateQueries({ queryKey: scansKey(slug) })
    },
  })

  if (!open) return null
  // A real dialog now: it used to render inline in the Danger zone, beside a
  // trigger that turned solid while open, though replay deletes nothing
  // (#247).
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <form
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={e => {
            e.preventDefault()
            setSubmitted(true)
            if (!from || !to) return
            replayMut.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Replay a past period</DialogTitle>
            <DialogDescription>
              Re-reads this period from the warehouse and rewrites its metric points. Nothing is deleted
              from your tracking plan.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor="replay-from">From</Label>
                <DateTimePicker id="replay-from" label="From" value={from} onChange={setFrom} aria-required {...invalidAria('replay-from', fromError)} className="w-full" />
                <FieldError inputId="replay-from" message={fromError} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="replay-to">To</Label>
                <DateTimePicker id="replay-to" label="To" value={to} onChange={setTo} aria-required {...invalidAria('replay-to', toError)} className="w-full" />
                <FieldError inputId="replay-to" message={toError} />
              </div>
            </div>
            {replayMut.isError && (
              <p role="alert" className="text-body text-danger">{getErrorMessage(replayMut.error)}</p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={replayMut.isPending}>
              <RotateCcw className="size-3.5" aria-hidden="true" />
              {replayMut.isPending ? 'Starting…' : 'Replay period'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
