import { Check, LayoutGrid } from 'lucide-react'
import type { FieldDefinition, MetaFieldDefinition } from '@/types'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { LAST_SEEN_COL_KEY, ROW_METRICS_LABEL } from './utils'
import { HEALTH_COL_KEY } from './useColumnVisibility'

export function ColumnsMenu({
  open,
  onOpenChange,
  tagsHidden,
  lastSeenHidden,
  fieldColumns,
  metaFields,
  hiddenColumns,
  offscreenColumnCount = 0,
  reviewedPinned = false,
  healthAvailable = false,
  onToggle,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  tagsHidden: boolean
  lastSeenHidden: boolean
  /** The review tab forces the Reviewed column on, so its toggle is inert there. */
  reviewedPinned?: boolean
  fieldColumns: FieldDefinition[]
  metaFields: MetaFieldDefinition[]
  hiddenColumns: Set<string>
  /** Columns scrolled outside the table's horizontal viewport right now. */
  offscreenColumnCount?: number
  /** The Health column exists on the main plan only (F15, #268). */
  healthAvailable?: boolean
  onToggle: (key: string) => void
}) {
  const statusHidden = hiddenColumns.has('status')
  // Not merely a toggle state: on the review tab the column is forced on, so it
  // is neither hidden nor counted as hidden.
  const reviewedHidden = hiddenColumns.has('reviewed') && !reviewedPinned
  const monitorHidden = hiddenColumns.has('monitor')
  const ownerHidden = hiddenColumns.has('owner')
  const deltaHidden = hiddenColumns.has('delta')
  const healthHidden = healthAvailable && hiddenColumns.has(HEALTH_COL_KEY)
  const totalHidden =
    (healthHidden ? 1 : 0) +
    (statusHidden ? 1 : 0) +
    (reviewedHidden ? 1 : 0) +
    (monitorHidden ? 1 : 0) +
    (ownerHidden ? 1 : 0) +
    (deltaHidden ? 1 : 0) +
    (tagsHidden ? 1 : 0) +
    (lastSeenHidden ? 1 : 0) +
    fieldColumns.filter((f) => hiddenColumns.has(`f:${f.id}`)).length +
    metaFields.filter((mf) => hiddenColumns.has(`m:${mf.id}`)).length
  // Toggled-off is only half the story: at 1512px the table is ~1665px wide in
  // a ~902px pane, so 8 further columns are merely scrolled out of sight. The
  // chip used to say "3 hidden" while 11 of 17 were unreadable, and it is the
  // only signal that the table continues to the right (tripl-u1ib).
  const badge = [
    totalHidden > 0 ? `${totalHidden} hidden` : null,
    offscreenColumnCount > 0 ? `${offscreenColumnCount} off-screen` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm">
          <LayoutGrid />
          <span className="max-sm:sr-only">Columns</span>
          {badge && (
            <span
              className="ml-1 text-micro text-fg-tertiary"
            >
              {badge}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-60 p-1.5">
        <div
          className="px-2 pb-1 pt-1.5 micro-label text-fg-tertiary"
        >
          Toggle columns
        </div>
        <ColumnToggle
          label="Status"
          pinned={false}
          checked={!statusHidden}
          onChange={() => onToggle('status')}
        />
        <ColumnToggle
          label="Verified"
          pinned={reviewedPinned}
          checked={!reviewedHidden}
          onChange={() => onToggle('reviewed')}
        />
        {/* Label follows the table heading ("Signal"); the persisted column key
            stays `monitor` so saved preferences survive the rename. */}
        <ColumnToggle
          label="Signal"
          pinned={false}
          checked={!monitorHidden}
          onChange={() => onToggle('monitor')}
        />
        {healthAvailable && (
          <ColumnToggle
            label="Health"
            pinned={false}
            checked={!healthHidden}
            onChange={() => onToggle(HEALTH_COL_KEY)}
          />
        )}
        <ColumnToggle
          label="Owner"
          pinned={false}
          checked={!ownerHidden}
          onChange={() => onToggle('owner')}
        />
        <ColumnToggle
          label="Δ · 24h"
          pinned={false}
          checked={!deltaHidden}
          onChange={() => onToggle('delta')}
        />
        <ColumnToggle
          label="Tags"
          pinned={false}
          checked={!tagsHidden}
          onChange={() => onToggle('tags')}
        />
        <ColumnToggle
          label="Last seen"
          pinned={false}
          checked={!lastSeenHidden}
          onChange={() => onToggle(LAST_SEEN_COL_KEY)}
        />
        {fieldColumns.length > 0 && (
          <>
            <div
              className="px-2 pb-1 pt-2 micro-label text-fg-tertiary"
            >
              Fields
            </div>
            {fieldColumns.map((f) => (
              <ColumnToggle
                key={f.id}
                label={f.display_name}
                pinned={false}
                checked={!hiddenColumns.has(`f:${f.id}`)}
                onChange={() => onToggle(`f:${f.id}`)}
              />
            ))}
          </>
        )}
        {metaFields.length > 0 && (
          <>
            <div
              className="px-2 pb-1 pt-2 micro-label text-fg-tertiary"
            >
              Meta
            </div>
            {metaFields.map((mf) => (
              <ColumnToggle
                key={mf.id}
                label={mf.display_name}
                pinned={false}
                checked={!hiddenColumns.has(`m:${mf.id}`)}
                onChange={() => onToggle(`m:${mf.id}`)}
              />
            ))}
          </>
        )}
        <div
          className="border-t px-2 pb-1 pt-2 text-micro border-border-subtle text-fg-tertiary"
        >
          Event stays pinned to the left edge while you scroll. Type and{' '}
          {ROW_METRICS_LABEL} cannot be hidden.
        </div>
      </PopoverContent>
    </Popover>
  )
}

function ColumnToggle({
  label,
  pinned,
  checked,
  onChange,
}: {
  label: string
  pinned: boolean
  checked: boolean
  onChange: () => void
}) {
  return (
    <button
      type="button"
      disabled={pinned}
      onClick={onChange}
      className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-body-sm hover:bg-[var(--surface-hover)] disabled:cursor-not-allowed"
      style={{ color: pinned ? 'var(--fg-faint)' : 'var(--fg)' }}
    >
      <span
        className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border"
        style={{
          background: checked ? 'var(--accent-solid)' : 'transparent',
          borderColor: checked ? 'var(--accent-solid)' : 'var(--border-strong)',
        }}
      >
        {checked && <Check className="size-3 text-accent-solid-fg" />}
      </span>
      <span className="flex-1 truncate">{label}</span>
      {pinned && (
        <span
          className="micro-label text-fg-tertiary"
        >
          pinned
        </span>
      )}
    </button>
  )
}
