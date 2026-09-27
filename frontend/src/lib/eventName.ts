/**
 * Display rules for an event name that would otherwise paint nothing.
 *
 * acme-ios held exactly one event whose stored name was the empty string, and
 * every surface rendered it as a zero-width anchor with no accessible name — the
 * one row a user would most want to clean up was the one row they could not
 * click, and a screen reader had nothing to announce (tripl-wkwv.5). Fixed once
 * here rather than at each call site that interpolates a name into an
 * aria-label, a title, a heading or a row.
 *
 * Not yet universal: ScanDetail.tsx and components/monitoring/metric-definition-card.tsx
 * still interpolate the raw name, so a blank one paints nothing there.
 */

/** What a surface prints in place of an event name that would paint nothing. */
export const UNNAMED_EVENT_LABEL = '(unnamed event)'

/**
 * An event name safe to put in an aria-label, a title, a heading or a row.
 *
 * Deliberately MORE generous than the scan-side skip in
 * `backend/src/tripl/core/analyzers/event_plan.py`: the scan skips only a name
 * that is exactly empty, because its gate has to agree with the metric
 * collector's `if event_name:` or a skipped identity comes back as a shadow
 * candidate. Here the only question is whether the reader sees anything, so a
 * whitespace-only name gets the placeholder too.
 *
 * A name with empty SEGMENTS (`::`, `onboarding:start:`) is a real identity and
 * passes through untouched — `EventName` paints its empty pieces as ∅, which is
 * a different defect with its own treatment.
 */
export function eventNameLabel(name: string | null | undefined): string {
  return name && name.trim() !== '' ? name : UNNAMED_EVENT_LABEL
}
