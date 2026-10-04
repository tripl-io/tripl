import { NativeSelect, Field } from '@/components/settings/kit'
import { METRIC_INTERVAL_LABEL } from '@/lib/metricFormat'
import { METRIC_SCAN_INTERVALS, type MetricScanInterval } from '@/types'

interface IntervalFieldProps {
  id: string
  value: MetricScanInterval
  onChange: (next: MetricScanInterval) => void
  /** The stored replay chunk the save re-sends, if any. */
  replayChunkInterval: MetricScanInterval | null
  /** The stored chunk the current interval suppresses (cleared on save), to say so. */
  clearedReplayChunk: MetricScanInterval | null
}

/**
 * Collection-interval row shared by SQL and fact metrics. It also accounts for
 * `replay_chunk_interval`, which this form never edits but always re-sends: a
 * chunk finer than the interval is a 422 naming a field the user cannot see,
 * so the form drops it when the interval passes it and says so here.
 */
export function IntervalField({
  id,
  value,
  onChange,
  replayChunkInterval,
  clearedReplayChunk,
}: IntervalFieldProps) {
  const hint = clearedReplayChunk
    ? `Backfill replay chunk (${METRIC_INTERVAL_LABEL[clearedReplayChunk].toLowerCase()}) was finer than this interval, so it will be cleared on save.`
    : replayChunkInterval
      ? `Backfills replay in ${METRIC_INTERVAL_LABEL[replayChunkInterval].toLowerCase()} chunks.`
      : undefined
  return (
    <Field label="Collection interval" htmlFor={id} required last hint={hint}>
      <NativeSelect
        id={id}
        value={value}
        onChange={next => onChange(next as MetricScanInterval)}
        options={METRIC_SCAN_INTERVALS.map(i => ({ value: i, label: METRIC_INTERVAL_LABEL[i] }))}
      />
    </Field>
  )
}
