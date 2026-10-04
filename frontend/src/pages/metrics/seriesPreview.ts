import type { MetricDraft } from './metricDraft'

/**
 * What a fact or event-composition draft still needs before its series can be
 * previewed, said under the button rather than leaving it silently grey.
 * Anything subtler — a measure the aggregation needs, a filter row
 * half filled — is the server's to name: the preview validates the definition
 * exactly as a save does.
 */
export function seriesPreviewBlocker(draft: MetricDraft): string | null {
  if (draft.kind === 'fact') {
    if (!draft.numeratorOp.factTableId) {
      return draft.factComposition === 'ratio'
        ? 'Pick the numerator fact table to preview.'
        : 'Pick a fact table to preview.'
    }
    if (draft.factComposition === 'ratio' && !draft.denominatorOp.factTableId) {
      return 'Pick the denominator fact table to preview.'
    }
    return null
  }
  if (draft.kind === 'event_composition') {
    if (!draft.numeratorEventId && !draft.numeratorEventTypeId) return 'Pick an event to preview.'
    if (draft.composition === 'ratio' && !draft.denominatorEventId && !draft.denominatorEventTypeId) {
      return 'Pick the event to divide by to preview.'
    }
    return null
  }
  return 'SQL metrics preview from the Query card.'
}

/** What the series preview covers, in the words of the kind being previewed. */
export function seriesPreviewScope(draft: MetricDraft): string {
  return draft.kind === 'fact'
    ? 'Dry-run over the last 50 buckets of the interval; nothing is saved.'
    : 'Composed from the counts scans already collected, over the newest 50 buckets; nothing is saved.'
}
