/** The branch-diff value renderer, lifted out of BranchesTab.tsx.
 *
 * Same constraint as `branchDiffFanout.ts`: BranchesTab.tsx has to stay
 * component-only for react-refresh, so the helpers these components are built
 * from — `inlineRecord`, `isFlatRecord`, `uniformRecords` — cannot be exported
 * from there. They live in `diffValueFormat.ts` beside this file, where a unit
 * test can reach them directly instead of mounting the whole tab behind a
 * router and four mocked APIs.
 *
 * The other half of the reason was size: BranchesTab.tsx was far past this
 * repo's 800-line ceiling, and this block was the largest piece of it that
 * owed the tab nothing. The rest of the tab has since been
 * split into ./branches/.
 */

import { useMemo, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Chip, type ChipTone } from '@/components/primitives/chip'
import {
  COLUMN_LABEL,
  JSON_CELL_MIN_LENGTH,
  inlineRecord,
  isFlatRecord,
  uniformRecords,
  type RecordRows,
} from './diffValueFormat'
import { wordDiff, type InlineSegment, type WordSegment } from './branches/wordDiff'

/**
 * A table cell whose value may itself be a JSON payload.
 *
 * `property` is the field that made the prose form unreadable in the first
 * place: a whole `{"from_profile": "${property.forecast_profile}", …}` object
 * spliced into the middle of the line. A table alone would just move the same
 * blow-out into one column, so a parseable object collapses to a one-line
 * preview with the pretty-printed form a click away. Template tokens survive
 * `JSON.parse` untouched — they sit inside string values.
 */
function ValueCell({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false)
  const text = value === null || value === '' ? '∅' : String(value)
  const parsed = useMemo(() => {
    const trimmed = text.trim()
    if (text.length < JSON_CELL_MIN_LENGTH) return null
    if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null
    try {
      return JSON.parse(trimmed) as unknown
    } catch {
      return null
    }
  }, [text])

  if (parsed === null) {
    return (
      <span className="mono wrap-anywhere text-caption text-fg">
        {text}
      </span>
    )
  }

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex min-w-0 items-center gap-1 text-left"
      >
        <ChevronRight
          className="size-3 shrink-0 transition-transform"
          style={{ color: 'var(--fg-faint)', transform: open ? 'rotate(90deg)' : 'none' }}
          aria-hidden="true"
        />
        <span className="mono truncate text-caption text-fg">
          {text}
        </span>
      </button>
      {open ? (
        <pre
          className="mono max-h-40 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-sm px-2 py-1 text-caption text-fg bg-fg/5"
        >
          {JSON.stringify(parsed, null, 2)}
        </pre>
      ) : null}
    </div>
  )
}

export function RecordTable({
  rows,
  labelledBy,
}: {
  rows: RecordRows
  labelledBy?: string
}) {
  // `is_authored` is not review signal — the backend filters a flip of it out of
  // change detection (_MEMBER_ATTRS_NOT_A_CHANGE), because it flips whenever a
  // person re-saves a scan-observed value unchanged. A column of it would take
  // width from the value, which is the cell that actually needs it, so it rides
  // along as a chip on the rows that came from a scan.
  const columns = Object.keys(rows[0]).filter((key) => key !== 'is_authored')
  const authoredKnown = 'is_authored' in rows[0]
  const chipColumn = columns.includes('value') ? 'value' : columns[columns.length - 1]
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {columns.map((key) => (
            <TableHead key={key} className="h-7 px-2 text-micro">
              {COLUMN_LABEL[key] ?? key.replace(/_/g, ' ')}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody aria-labelledby={labelledBy}>
        {rows.map((row, idx) => (
          <TableRow key={idx}>
            {columns.map((key) => (
              <TableCell key={key} className="px-2 py-1 align-top">
                <div className="flex min-w-0 items-start gap-2">
                  <ValueCell value={row[key]} />
                  {authoredKnown && key === chipColumn && row.is_authored === false ? (
                    <Chip tone="neutral" size="xs" className="shrink-0">
                      from scan
                    </Chip>
                  ) : null}
                </div>
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** Renders a diff value the way a reviewer reads it: scalars plain, lists
 * comma-joined, flat records as inline `key: value` pairs, and nested records
 * one per line. Pretty-printed JSON is the last resort, not the default.
 * `tone` colours the before (danger) / after (success) sides of a diff.
 *
 * `wrap-anywhere` (overflow-wrap: anywhere) rather than `break-words`
 * (overflow-wrap: break-word): only the former reduces the element's
 * MIN-CONTENT width. `break-words` wraps once the box is already constrained,
 * but it still reports a long unbroken token as the minimum the box needs, so
 * inside an auto-minimum track it grows the column instead of wrapping — which
 * is how one long event description made the whole page scroll sideways. */
export function DiffValue({
  value,
  tone,
  table,
  labelledBy,
}: {
  value: unknown
  tone?: ChipTone
  /** Render a uniform array of records as a real table. Only the full-state
   * view asks for this; see the comment at its call site. */
  table?: boolean
  labelledBy?: string
}) {
  const color = tone ? `var(--${tone})` : 'var(--fg)'
  const isEmpty =
    value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)
  if (isEmpty) {
    return (
      <span className="mono text-caption text-fg-tertiary">
        ∅
      </span>
    )
  }
  if (Array.isArray(value)) {
    if (value.every((item) => typeof item !== 'object' || item === null)) {
      return (
        <span className="mono wrap-anywhere text-caption" style={{ color }}>
          {value.map((item) => String(item)).join(', ')}
        </span>
      )
    }
    if (table) {
      const rows = uniformRecords(value)
      if (rows) return <RecordTable rows={rows} labelledBy={labelledBy} />
    }
    if (value.every(isFlatRecord)) {
      return (
        <div className="flex flex-col gap-0.5">
          {value.map((item, idx) => (
            <span
              key={idx}
              className="mono wrap-anywhere text-caption"
              style={{ color }}
            >
              {inlineRecord(item)}
            </span>
          ))}
        </div>
      )
    }
  }
  if (isFlatRecord(value)) {
    return (
      <span className="mono wrap-anywhere text-caption" style={{ color }}>
        {inlineRecord(value)}
      </span>
    )
  }
  if (typeof value === 'object') {
    return (
      <pre
        className="mono max-h-40 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-sm px-2 py-1 text-caption"
        style={{ color, background: 'color-mix(in oklab, var(--fg) 5%, transparent)' }}
      >
        {JSON.stringify(value, null, 2)}
      </pre>
    )
  }
  return (
    <span className="mono wrap-anywhere text-caption" style={{ color }}>
      {String(value)}
    </span>
  )
}

/** One side of a word-diffed pair: the shared words plain, the others marked
 * as a real <del>/<ins>, so the difference is not carried by colour alone. */
function WordDiffText({ segments, side }: { segments: WordSegment[]; side: 'before' | 'after' }) {
  const tone = side === 'before' ? 'danger' : 'success'
  const Mark = side === 'before' ? 'del' : 'ins'
  return (
    <span
      className="mono wrap-anywhere whitespace-pre-wrap text-caption text-fg"
    >
      {segments.map((segment, index) =>
        segment.changed ? (
          <Mark
            key={index}
            className="rounded-sm px-0.5"
            style={{
              color: `var(--${tone})`,
              background: `color-mix(in oklab, var(--${tone}) 14%, transparent)`,
              textDecoration: side === 'before' ? 'line-through' : 'none',
            }}
          >
            {segment.text}
          </Mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </span>
  )
}

/** The merged paragraph: shared text once, removed words struck through in
 * the danger tint, added words in the success tint. Sans, because it
 * is prose; the marks are real <del>/<ins>. */
function InlineWordDiff({ segments }: { segments: InlineSegment[] }) {
  return (
    <span className="whitespace-pre-wrap break-words text-body-sm text-fg">
      {segments.map((segment, index) => {
        if (segment.kind === 'same') return <span key={index}>{segment.text}</span>
        const tone = segment.kind === 'removed' ? 'danger' : 'success'
        const Mark = segment.kind === 'removed' ? 'del' : 'ins'
        return (
          <Mark
            key={index}
            className="rounded-sm px-0.5"
            style={{
              color: `var(--${tone})`,
              background: `color-mix(in oklab, var(--${tone}) 14%, transparent)`,
              textDecoration: segment.kind === 'removed' ? 'line-through' : 'none',
            }}
          >
            {segment.text}
          </Mark>
        )
      })}
    </span>
  )
}

/**
 * A before → after pair, as a reviewer reads it.
 *
 * The two sides used to differ only in colour, so each now carries a visually
 * hidden "before:" / "after:" for a screen reader. Two long strings — a
 * rewritten description — are word-diffed. Prose reads as ONE paragraph in the
 * body font with the removed and added words marked in place, instead of the
 * whole sentence twice in monospace with an arrow dangling between them;
 * "Show before / after" brings the two-sided view back. Short
 * identifiers and enum values keep the mono `a → b`.
 */
export function DiffPair({ before, after }: { before: unknown; after: unknown }) {
  const [sideBySide, setSideBySide] = useState(false)
  const words =
    typeof before === 'string' && typeof after === 'string' ? wordDiff(before, after) : null
  if (words && !sideBySide) {
    return (
      <span className="flex w-full min-w-0 flex-col items-start gap-1">
        {/* A screen reader hears the two whole values; the marked paragraph is
            for the eye. */}
        <span className="sr-only">before:</span>
        <span className="sr-only">{String(before)}</span>
        <span className="sr-only">after:</span>
        <span className="sr-only">{String(after)}</span>
        <span aria-hidden="true">
          <InlineWordDiff segments={words.inline} />
        </span>
        <button
          type="button"
          onClick={() => setSideBySide(true)}
          className="text-caption hover:underline text-fg-tertiary"
        >
          Show before / after
        </button>
      </span>
    )
  }
  return (
    <>
      <span className="sr-only">before:</span>
      {words ? (
        <WordDiffText segments={words.before} side="before" />
      ) : (
        <DiffValue value={before} tone="danger" />
      )}
      <span className="text-body-sm text-fg-tertiary" aria-hidden="true">
        →
      </span>
      <span className="sr-only">after:</span>
      {words ? (
        <WordDiffText segments={words.after} side="after" />
      ) : (
        <DiffValue value={after} tone="success" />
      )}
    </>
  )
}
