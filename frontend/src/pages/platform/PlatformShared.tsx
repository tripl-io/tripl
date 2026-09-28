import type { PlatformOrgStatus } from '@/api/platform'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { PLATFORM_PAGE_SIZE, STATUS_LABEL, STATUS_TONE, rangeLabel } from './platformModel'

/** An organization's status as a pill. */
export function OrgStatusChip({ status }: { status: PlatformOrgStatus }) {
  return <Chip tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Chip>
}

/** "Showing 1–50 of 230" with Previous / Next, under a console list. */
export function Pager({
  offset,
  shown,
  total,
  onOffset,
  label,
}: {
  offset: number
  shown: number
  total: number
  onOffset: (next: number) => void
  /** Names the pager for assistive tech ("Organizations pages"). */
  label: string
}) {
  const hasPrev = offset > 0
  const hasNext = offset + shown < total
  return (
    <nav aria-label={label} className="flex items-center justify-between gap-3 px-4 py-3">
      <span className="text-body-sm text-fg-tertiary">{rangeLabel(offset, shown, total)}</span>
      {(hasPrev || hasNext) && (
        <span className="flex gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!hasPrev}
            onClick={() => onOffset(Math.max(0, offset - PLATFORM_PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!hasNext}
            onClick={() => onOffset(offset + PLATFORM_PAGE_SIZE)}
          >
            Next
          </Button>
        </span>
      )}
    </nav>
  )
}
