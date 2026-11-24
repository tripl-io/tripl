import type { MetricPreviewPoint } from '@/types'

/**
 * One bucket of a saved-vs-draft comparison.
 *
 * `saved` / `draft` are `null` when that side has NO point for the bucket —
 * a missing bucket — which is a different fact from a point whose value is
 * `0`. Change figures exist only when both sides have a value.
 */
export interface CompareRow {
  /** Bucket start in epoch milliseconds, the alignment key. */
  bucketMs: number
  saved: number | null
  draft: number | null
  /** `draft - saved`; `null` unless both sides have a value. */
  absChange: number | null
  /**
   * `absChange / |saved| * 100`; `null` when the change is undefined: a missing
   * side, or a zero baseline (no percentage of zero).
   */
  pctChange: number | null
}

export interface CompareSummary {
  rows: CompareRow[]
  /** Buckets where both sides have a value. */
  comparedCount: number
  /** Buckets present on only one side (or neither). */
  missingCount: number
  /** Sum of `saved` and `draft` across compared buckets. */
  savedTotal: number
  draftTotal: number
  /** `draftTotal - savedTotal`, the net shift the KPI would see in this window. */
  netAbsChange: number
  /** Net shift as a percentage of `savedTotal`; `null` for a zero baseline. */
  netPctChange: number | null
}

function bucketMs(point: MetricPreviewPoint): number {
  return new Date(point.bucket).getTime()
}

/**
 * Align two previews by bucket and describe how the draft shifts each one.
 * Pure: nothing is saved or fetched here, so comparing can never change the
 * saved definition or its collected series.
 */
export function compareSeries(
  saved: readonly MetricPreviewPoint[],
  draft: readonly MetricPreviewPoint[],
): CompareSummary {
  const savedByBucket = new Map<number, number>()
  for (const point of saved) savedByBucket.set(bucketMs(point), point.value)
  const draftByBucket = new Map<number, number>()
  for (const point of draft) draftByBucket.set(bucketMs(point), point.value)

  const buckets = [...new Set([...savedByBucket.keys(), ...draftByBucket.keys()])].sort(
    (a, b) => a - b,
  )

  const rows: CompareRow[] = buckets.map(key => {
    const savedValue = savedByBucket.get(key) ?? null
    const draftValue = draftByBucket.get(key) ?? null
    if (savedValue === null || draftValue === null) {
      return { bucketMs: key, saved: savedValue, draft: draftValue, absChange: null, pctChange: null }
    }
    const absChange = draftValue - savedValue
    const pctChange = savedValue === 0 ? null : (absChange / Math.abs(savedValue)) * 100
    return { bucketMs: key, saved: savedValue, draft: draftValue, absChange, pctChange }
  })

  let comparedCount = 0
  let missingCount = 0
  let savedTotal = 0
  let draftTotal = 0
  for (const row of rows) {
    if (row.saved === null || row.draft === null) {
      missingCount += 1
      continue
    }
    comparedCount += 1
    savedTotal += row.saved
    draftTotal += row.draft
  }
  const netAbsChange = draftTotal - savedTotal
  return {
    rows,
    comparedCount,
    missingCount,
    savedTotal,
    draftTotal,
    netAbsChange,
    netPctChange: savedTotal === 0 ? null : (netAbsChange / Math.abs(savedTotal)) * 100,
  }
}
