import type { CSSProperties, ReactNode, RefObject } from 'react'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

/**
 * The option list of a text combobox, portalled and anchored to its field.
 *
 * Inline `absolute z-50` lists were cut off by any card with rounded-corner
 * clipping and fought other layers for z-order; ColumnSuggest moved to a Radix
 * Popover for that and the variable, JSON and template editors share the
 * same behaviour through this. The field keeps focus throughout: the
 * popover neither takes focus on open nor hands it back on close, and a press
 * on the anchor itself is not an outside press. The caller owns the options,
 * `aria-activedescendant` and keyboard handling on its field.
 */
export function AnchoredListbox({
  id,
  open,
  anchorRef,
  onDismiss,
  ariaLabel,
  className,
  style,
  children,
}: {
  id: string
  open: boolean
  /** The element the list lines up under — the field or its wrapper. */
  anchorRef: RefObject<HTMLElement | null>
  /** Escape, or a press outside both the field and the list. */
  onDismiss: () => void
  ariaLabel?: string
  className?: string
  style?: CSSProperties
  children: ReactNode
}) {
  return (
    <Popover open={open} onOpenChange={next => { if (!next) onDismiss() }}>
      <PopoverAnchor virtualRef={anchorRef} />
      <PopoverContent
        id={id}
        role="listbox"
        aria-label={ariaLabel}
        align="start"
        sideOffset={4}
        onOpenAutoFocus={e => e.preventDefault()}
        onCloseAutoFocus={e => e.preventDefault()}
        onInteractOutside={e => {
          if (anchorRef.current?.contains(e.target as Node)) e.preventDefault()
        }}
        className={cn('max-h-64 w-(--radix-popover-trigger-width) overflow-y-auto p-1', className)}
        style={style}
      >
        {children}
      </PopoverContent>
    </Popover>
  )
}
