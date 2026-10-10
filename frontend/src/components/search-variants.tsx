import { useId, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Command } from 'cmdk'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { PALETTE_ITEM_CLASS } from '@/components/palette-item'
import { cn } from '@/lib/utils'
import type { SearchVariant, SearchVariantGroup } from '@/types'
import { countOf } from '@/lib/plural'

/**
 * Variant groups in the command palette's search results (#238).
 *
 * The search service folds events of one event type whose names differ only in
 * one naming-rule placeholder — `screen=Home`, `screen=Map`, … — under the
 * best-ranked of them. The palette used to list every one, so a query for a
 * screen name read as the exact event followed by eight look-alikes. Now the
 * representative carries a "+ N variants" count, and one row beneath it
 * expands the rest in place.
 *
 * The expand control is a cmdk item of its own rather than a button inside the
 * representative's row: cmdk keeps DOM focus in the search input and moves the
 * selection with the arrow keys, so a nested button would be reachable only by
 * Tab and would steal the representative's click. As an item it is arrowed to
 * and toggled with Enter like every other row.
 *
 * Being an `option`, it cannot carry `aria-expanded` (ARIA 1.2 allows it on
 * neither `option` nor anything nested in one; axe fails it as
 * aria-allowed-attr). The state is in its name instead — "Show 2 variants" /
 * "Hide 2 variants" — with `aria-controls` naming the members' group and
 * `data-expanded` for styling and tests. A name change on the active option is
 * not reliably re-read, so each toggle is also announced through a polite
 * status region. It is portalled to `<body>`: a listbox may own only options
 * and groups, so the region cannot sit inside the list.
 */

/**
 * The "+ N variants" suffix on a representative's label. The leading space
 * sits outside the styled span, so the accessible name reads
 * "Home + 2 variants" and not "Home+ 2 variants".
 */
export function SearchVariantCount({ count }: { count: number }) {
  return (
    <>
      {' '}
      <span className="text-fg-tertiary">
        + {countOf(count, 'variant', 'variants')}
      </span>
    </>
  )
}

interface SearchVariantRowsProps {
  group: SearchVariantGroup
  /** The representative's own row; rendered first, unchanged. */
  representative: ReactNode
  /** The expand row's cmdk value. Must be unique in the list. */
  toggleValue: string
  /** One folded member's row. Only called while the group is expanded. */
  renderVariant: (variant: SearchVariant) => ReactNode
}

/**
 * A representative, its expand row, and — once expanded — every folded member.
 *
 * Collapsed, the members are not rendered at all rather than hidden: cmdk
 * registers every mounted item for arrow-key navigation, so hidden rows would
 * still be stepped through.
 */
export function SearchVariantRows({
  group,
  representative,
  toggleValue,
  renderVariant,
}: SearchVariantRowsProps) {
  const [expanded, setExpanded] = useState(false)
  // Empty until the first toggle, so opening the palette announces nothing.
  const [announcement, setAnnouncement] = useState('')
  const membersId = useId()
  const count = group.variants.length
  const Chevron = expanded ? ChevronDown : ChevronRight
  const toggle = () => {
    const next = !expanded
    setExpanded(next)
    setAnnouncement(
      `${countOf(count, 'variant', 'variants')} of ${group.pattern} ${next ? 'expanded' : 'collapsed'}`,
    )
  }
  return (
    <>
      {representative}
      <Command.Item
        value={toggleValue}
        onSelect={toggle}
        aria-controls={membersId}
        data-expanded={expanded}
        className={cn(PALETTE_ITEM_CLASS, 'pl-7 text-fg-secondary')}
      >
        <Chevron className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate">
          {expanded ? 'Hide' : 'Show'} {countOf(count, 'variant', 'variants')}
        </span>
        <span className="mono hidden max-w-[40%] shrink-0 truncate text-micro text-fg-tertiary sm:block">
          {`{${group.placeholder}}`}
        </span>
      </Command.Item>
      <div
        id={membersId}
        role="group"
        aria-label={`Variants of ${group.pattern}`}
        hidden={!expanded}
        className="pl-5"
      >
        {expanded && group.variants.map(variant => renderVariant(variant))}
      </div>
      {createPortal(
        <div role="status" className="sr-only">
          {announcement}
        </div>,
        document.body,
      )}
    </>
  )
}
