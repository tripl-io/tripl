import { ArrowRightLeft, Copy, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { BranchTransferMode } from '@/api/planBranches'

interface TransferBarProps {
  count: number
  /** A closed branch can only be copied from; a merged one offers nothing. */
  canMove: boolean
  onTransfer: (mode: BranchTransferMode) => void
  onClear: () => void
}

/** "N selected · Move to branch… · Copy to branch…", stuck under the list. */
export function TransferBar({ count, canMove, onTransfer, onClear }: TransferBarProps) {
  if (count === 0) return null
  return (
    <div
      role="toolbar"
      aria-label="Selected changes"
      className="sticky bottom-0 z-10 flex flex-wrap items-center gap-2 border-t px-4 py-2.5 border-border-subtle bg-surface"
    >
      <span className="text-body-sm text-fg" aria-live="polite">
        {count} selected
      </span>
      <span className="flex-1" aria-hidden="true" />
      {canMove ? (
        <Button type="button" size="sm" onClick={() => onTransfer('move')}>
          <ArrowRightLeft aria-hidden="true" />
          Move to branch…
        </Button>
      ) : null}
      <Button type="button" size="sm" variant="outline" onClick={() => onTransfer('copy')}>
        <Copy aria-hidden="true" />
        Copy to branch…
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={onClear} aria-label="Clear selection">
        <X aria-hidden="true" />
      </Button>
    </div>
  )
}
