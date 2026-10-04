import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { INPUT_BASE, INPUT_CLASS, INPUT_DISABLED } from '@/components/settings/input-style'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'

/**
 * Column-name input backed by data-source schema suggestions. Free typing is
 * always allowed — the schema cache may lag the warehouse — so the listbox only
 * assists, never restricts. Follows the accessible combobox pattern already
 * used by pages/events/VariableInput (role="combobox" + listbox options).
 */

interface ColumnSuggestInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  /** Candidate column names; an empty list makes this a plain text input. */
  suggestions: string[]
  placeholder?: string
  disabled?: boolean
  'aria-label'?: string
  /** Set by a form row that is showing a validation message for this input. */
  'aria-invalid'?: boolean
  'aria-describedby'?: string
  'aria-required'?: boolean
}

/**
 * Controlled mono text input with a substring-filtered suggestion listbox.
 * ArrowUp/ArrowDown move the highlight, Enter picks it, Escape closes; clicking
 * an option picks it without losing focus.
 */
export function ColumnSuggestInput({
  id,
  value,
  onChange,
  suggestions,
  placeholder,
  disabled,
  'aria-label': ariaLabel,
  'aria-invalid': ariaInvalid,
  'aria-describedby': ariaDescribedBy,
  'aria-required': ariaRequired,
}: ColumnSuggestInputProps) {
  const uid = useId()
  const listboxId = `column-listbox-${uid}`
  const inputRef = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)

  const filtered = useMemo(() => {
    const query = value.trim().toLowerCase()
    return suggestions.filter(name => name.toLowerCase().includes(query))
  }, [suggestions, value])

  const expanded = open && filtered.length > 0
  // The highlight index can outlive a shrinking filter; clamp instead of
  // resetting so ArrowUp/Down stay stable while typing.
  const activeIdx = Math.min(highlight, filtered.length - 1)

  // Keep the highlighted option visible: the list scrolls at ~8 rows, and
  // ArrowDown past that used to highlight options nobody could see (DS-35).
  // Optional call — jsdom has no scrollIntoView.
  useEffect(() => {
    if (!expanded) return
    document.getElementById(`${listboxId}-opt-${activeIdx}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [expanded, activeIdx, listboxId])

  const pick = (name: string) => {
    onChange(name)
    setOpen(false)
    setHighlight(0)
  }

  const handleChange = (next: string) => {
    onChange(next)
    setOpen(true)
    setHighlight(0)
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!expanded) {
      if (e.key === 'ArrowDown' && filtered.length > 0) {
        e.preventDefault()
        setOpen(true)
        setHighlight(0)
      }
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      const choice = filtered[activeIdx]
      if (choice !== undefined) pick(choice)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight(Math.min(activeIdx + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight(Math.max(activeIdx - 1, 0))
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
    } else if (e.key === 'Tab') {
      setOpen(false)
    }
  }

  // The list is portalled (Radix Popover anchored to the input) rather than
  // positioned inside the field: in the last Field of an SCard it was cut off
  // by the card's rounded-corner clip almost entirely, while the combobox still
  // reported itself expanded (DS-3). Focus never leaves the input — the popover
  // neither takes it on open nor hands it back on close.
  return (
    <Popover open={expanded} onOpenChange={next => { if (!next) setOpen(false) }}>
      <PopoverAnchor asChild>
        <input
          ref={inputRef}
          id={id}
          type="text"
          role="combobox"
          aria-expanded={expanded}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-activedescendant={expanded ? `${listboxId}-opt-${activeIdx}` : undefined}
          aria-label={ariaLabel}
          aria-invalid={ariaInvalid || undefined}
          aria-describedby={ariaDescribedBy}
          aria-required={ariaRequired}
          autoComplete="off"
          // The shared invalid edge + halo and the faint placeholder (MT-7):
          // aria-invalid alone drew nothing on this hand-rolled control.
          className={`${INPUT_CLASS} mono`}
          // The disabled cue comes from the shared primitive, not from a local
          // knock-down. This box used to dim itself with `opacity: 0.6`, the same
          // treatment that on the dark theme left a dead field 3/255 of fill and
          // 7/255 of border away from a live one — indistinguishable in a
          // screenshot. INPUT_DISABLED is a shape change (no well,
          // dashed border) precisely so it does not depend on that delta.
          style={{ ...INPUT_BASE, ...(disabled ? INPUT_DISABLED : {}) }}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={e => handleChange(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setOpen(true)}
        />
      </PopoverAnchor>
      <PopoverContent
        id={listboxId}
        role="listbox"
        aria-label="Column suggestions"
        align="start"
        sideOffset={4}
        onOpenAutoFocus={e => e.preventDefault()}
        onCloseAutoFocus={e => e.preventDefault()}
        // A press on the input itself is not "outside": it is where typing
        // happens, and closing there would flicker the list on every click.
        onInteractOutside={e => {
          if (inputRef.current?.contains(e.target as Node)) e.preventDefault()
        }}
        className="max-h-[220px] w-(--radix-popover-trigger-width) overflow-y-auto rounded-control p-1 bg-surface border-border"
      >
        {filtered.map((name, i) => (
          <button
            key={name}
            id={`${listboxId}-opt-${i}`}
            type="button"
            role="option"
            tabIndex={-1}
            aria-selected={i === activeIdx}
            onMouseDown={e => e.preventDefault()}
            onClick={() => pick(name)}
            onMouseEnter={() => setHighlight(i)}
            className="mono flex w-full items-center rounded-control px-2 py-[5px] text-left text-body-sm"
            style={{
              background: i === activeIdx ? 'var(--surface-hover)' : 'transparent',
              color: 'var(--fg)',
            }}
          >
            {name}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  )
}
