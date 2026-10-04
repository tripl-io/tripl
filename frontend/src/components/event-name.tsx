import { Fragment } from 'react'
import type { ReactNode } from 'react'
import { UNNAMED_EVENT_LABEL } from '@/lib/eventName'
import { NAME_SEGMENT_SEPARATOR, splitEventName } from '@/lib/eventNameSegments'

function EmptySegment(): ReactNode {
  return (
    <span title="empty segment" className="text-fg-tertiary">
      ∅
    </span>
  )
}

/**
 * Renders an event name, covering TWO distinct defects the catalog holds:
 *
 * - a name that is MISSING entirely, which used to render as nothing at all —
 *   a zero-width anchor with no accessible name. It becomes the
 *   muted, italic `(unnamed event)` placeholder below. Real text, deliberately
 *   NOT `aria-hidden`: the whole point is that the enclosing link gets an
 *   accessible name and a clickable area.
 * - a name with empty SEGMENTS (e.g. "spot::services"), which is a real
 *   identity and keeps its intentional muted ∅ per empty piece rather than a
 *   silent gap.
 *
 * Ordinary names render unchanged. Shared by the Events list and the
 * Reconciliation page so glitchy names read identically across surfaces.
 */
export function EventName({ name }: { name: string }): ReactNode {
  if (!name || name.trim() === '') {
    return (
      <span className="italic text-fg-tertiary">
        {UNNAMED_EVENT_LABEL}
      </span>
    )
  }
  const segments = splitEventName(name)
  if (!segments) return name
  return segments.map((seg, i) => (
    <Fragment key={i}>
      {i > 0 && (
        <span aria-hidden className="text-fg-tertiary">
          {NAME_SEGMENT_SEPARATOR}
        </span>
      )}
      {seg.empty ? <EmptySegment /> : <span>{seg.text}</span>}
    </Fragment>
  ))
}
