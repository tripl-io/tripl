import { Loader2 } from 'lucide-react'
import { DOC_LINK_KIND_NOUN } from '@/lib/docLinks'
import { cn } from '@/lib/utils'
import { DOC_LINK_KIND_ICON } from './docLinkIcons'
import type { DocLinkPicker } from './useDocLinkPicker'

/** Where the popup sits: under the `[[` / `@`, in viewport pixels. */
export interface PickerPosition {
  left: number
  top: number
}

/**
 * The `[[` / `@` suggestion list (F24). The editor keeps focus the whole
 * time: its `aria-controls` / `aria-activedescendant` point here, and it
 * forwards ↑ ↓ Enter Tab Esc to the picker, so the rows never take
 * focus themselves; a mouse pick keeps focus in the editor too.
 */
export function DocLinkPickerPopup({
  picker,
  position,
}: {
  picker: DocLinkPicker
  /** Null when the caret position is unknown: the list sits under the editor. */
  position: PickerPosition | null
}) {
  const { trigger, items, loading, active } = picker
  if (!trigger) return null

  const heading =
    trigger.mode === 'mention'
      ? 'Mention a person'
      : trigger.kind
        ? `Link to a ${DOC_LINK_KIND_NOUN[trigger.kind]}`
        : 'Link to a note, plan entity, alert rule or person'
  const status = loading && items.length === 0
    ? 'Searching…'
    : items.length === 0
      ? trigger.query ? 'No matches' : 'Type to search'
      : `${items.length} ${items.length === 1 ? 'suggestion' : 'suggestions'}`

  return (
    <div
      data-slot="doc-link-picker"
      className={cn(
        'z-50 w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-control border border-border bg-surface shadow-lg',
        position ? 'fixed' : 'absolute left-2 top-full mt-1',
      )}
      style={position ? { left: position.left, top: position.top } : undefined}
    >
      <div className="flex items-center justify-between gap-2 border-b border-border-subtle px-2.5 py-1.5">
        <span className="micro-label text-fg-tertiary">{heading}</span>
        {loading && <Loader2 className="size-3 animate-spin text-fg-tertiary" aria-hidden />}
      </div>
      {items.length > 0 && (
        <div
          id={picker.listId}
          role="listbox"
          aria-label={heading}
          className="max-h-64 overflow-y-auto py-1"
        >
          {items.map((item, index) => {
            const Icon = DOC_LINK_KIND_ICON[item.kind]
            const selected = index === active
            return (
              <div
                key={`${item.kind}:${item.id}`}
                id={picker.optionId(index)}
                role="option"
                tabIndex={-1}
                aria-selected={selected}
                data-kind={item.kind}
                // mousedown, not click: the editor must not lose focus (and the
                // trigger with it) before the pick lands.
                onMouseDown={event => {
                  event.preventDefault()
                  picker.select(index)
                }}
                onMouseEnter={() => picker.setActive(index)}
                className={cn(
                  'flex cursor-pointer items-start gap-2 px-2.5 py-1.5 text-body-sm',
                  selected ? 'bg-[var(--accent-soft)] text-fg' : 'text-fg-secondary',
                )}
              >
                <Icon className="mt-0.5 size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium text-fg">
                    {item.kind === 'user' ? `@${item.label}` : item.label}
                  </span>
                  {item.detail && <span className="block truncate text-caption text-fg-tertiary">{item.detail}</span>}
                </span>
                <span className="shrink-0 text-caption text-fg-tertiary">{DOC_LINK_KIND_NOUN[item.kind]}</span>
              </div>
            )
          })}
        </div>
      )}
      {items.length === 0 && <p className="m-0 px-2.5 pb-2 text-caption text-fg-tertiary">{status}</p>}
      <p role="status" aria-live="polite" className="sr-only">
        {status}
      </p>
      <p className="m-0 border-t border-border-subtle px-2.5 py-1 text-caption text-fg-tertiary">
        ↑ ↓ to choose · Enter to insert · Esc to close
      </p>
    </div>
  )
}
