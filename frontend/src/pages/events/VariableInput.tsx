import { type RefObject, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { AnchoredListbox } from '@/components/ui/anchored-listbox'
import { cn } from '@/lib/utils'
import { useEvDescribedBy } from './evFieldContext'
import { TEXT_INPUT_CLASS } from './eventFormLayout'
import { filterVariableSuggestions, type VariableSuggestion } from './variableSuggestions'

export type { VariableSuggestion }

export function SuggestionRow({ suggestion }: { suggestion: VariableSuggestion }) {
  const bindings = suggestion.bindings ?? []
  const values = suggestion.allowed_values ?? []
  const detailClassName = 'text-fg-tertiary/80'
  return (
    <>
      <code className="shrink-0 font-mono text-primary">
        {`\${${suggestion.name}}`}
      </code>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5 overflow-hidden text-left">
        {suggestion.description && (
          <span
            title={suggestion.description}
            className="w-full truncate text-fg-tertiary"
          >
            {suggestion.description}
          </span>
        )}
        {bindings.length > 0 && (
          <span className={`w-full truncate font-mono text-micro ${detailClassName}`}>{bindings.join(' · ')}</span>
        )}
        {values.length > 0 && (
          <span className={`w-full truncate font-mono text-micro ${detailClassName}`}>{values.slice(0, 3).join(' · ')}</span>
        )}
      </span>
    </>
  )
}

/**
 * The dropdown under a variable-aware input, shared by the single-line input and
 * the JSON editor so the two cannot drift. Height-limited and scrolling, and the
 * highlighted option is kept in view as the arrow keys move it (EVT-24). It is
 * portalled and anchored to the field, so a clipping card no longer cuts it
 * off (DS-35); the highlight is the neutral hover surface, not the brand
 * colour (DS-10).
 */
export function SuggestionListbox({
  id,
  open,
  anchorRef,
  onDismiss,
  suggestions,
  highlightIdx,
  onPick,
}: {
  id: string
  open: boolean
  anchorRef: RefObject<HTMLElement | null>
  onDismiss: () => void
  suggestions: VariableSuggestion[]
  highlightIdx: number
  onPick: (name: string) => void
}) {
  useEffect(() => {
    if (!open) return
    const active = document.getElementById(`${id}-opt-${highlightIdx}`)
    // Optional call: jsdom does not implement scrollIntoView.
    active?.scrollIntoView?.({ block: 'nearest' })
  }, [id, open, highlightIdx])
  return (
    <AnchoredListbox id={id} open={open} anchorRef={anchorRef} onDismiss={onDismiss} ariaLabel="Properties">
      {suggestions.map((v, i) => (
        <button
          key={v.name}
          id={`${id}-opt-${i}`}
          type="button"
          role="option"
          tabIndex={-1}
          aria-selected={i === highlightIdx}
          onMouseDown={e => { e.preventDefault(); onPick(v.name) }}
          className={`flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-body-sm ${i === highlightIdx ? 'bg-surface-hover text-foreground' : 'text-popover-foreground hover:bg-surface-hover'}`}
        >
          <SuggestionRow suggestion={v} />
        </button>
      ))}
    </AnchoredListbox>
  )
}

// Input types that may carry role="combobox" (ARIA in HTML). A date or number
// input is a different widget, and the autocomplete means nothing on it.
const COMBOBOX_TYPES = new Set(['text', 'search', 'url', 'email', 'tel'])

export function VariableInput({
  id,
  value,
  onChange,
  variables,
  required,
  ariaRequired,
  type = 'text',
  inputMode,
  className,
  invalid,
  describedBy: ownDescribedBy,
  ariaLabel,
}: {
  id?: string
  value: string
  onChange: (v: string) => void
  variables: VariableSuggestion[]
  required?: boolean
  /** Announced as required without the browser enforcing it. */
  ariaRequired?: boolean
  type?: string
  inputMode?: 'text' | 'decimal' | 'numeric' | 'url'
  className?: string
  invalid?: boolean
  /** Ids of this control's own messages, merged with its form row's. */
  describedBy?: string
  /** For a control with no <label> of its own (a JSON grid cell). */
  ariaLabel?: string
}) {
  const uid = useId()
  const listboxId = `variable-listbox-${uid}`
  const ref = useRef<HTMLInputElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const [showMenu, setShowMenu] = useState(false)
  const [filter, setFilter] = useState('')
  const [highlightIdx, setHighlightIdx] = useState(0)
  const [insertPos, setInsertPos] = useState(0)
  const describedBy = useEvDescribedBy(ownDescribedBy)
  const combobox = COMBOBOX_TYPES.has(type)

  const filtered = useMemo(
    () => (combobox ? filterVariableSuggestions(variables, filter) : []),
    [combobox, variables, filter],
  )
  const open = showMenu && filtered.length > 0

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      // The list is portalled, so a press on it (its scrollbar) is outside the wrapper.
      if (document.getElementById(listboxId)?.contains(target)) return
      if (wrapperRef.current && !wrapperRef.current.contains(target)) setShowMenu(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [listboxId])

  const insert = useCallback((varName: string) => {
    const before = value.slice(0, insertPos)
    const after = value.slice(ref.current?.selectionEnd ?? insertPos)
    const dollarIdx = before.lastIndexOf('$')
    const newValue = before.slice(0, dollarIdx) + '${' + varName + '}' + after
    onChange(newValue)
    setShowMenu(false)
    setTimeout(() => {
      const pos = dollarIdx + varName.length + 3
      ref.current?.setSelectionRange(pos, pos)
      ref.current?.focus()
    }, 0)
  }, [value, insertPos, onChange])

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value
    const cursor = e.target.selectionStart ?? v.length
    onChange(v)

    const before = v.slice(0, cursor)
    const dollarIdx = before.lastIndexOf('$')
    if (dollarIdx >= 0) {
      const afterDollar = before.slice(dollarIdx + 1)
      if (!afterDollar.includes('}') && !/\s/.test(afterDollar)) {
        setFilter(afterDollar.replace(/^\{/, ''))
        setInsertPos(cursor)
        setShowMenu(true)
        setHighlightIdx(0)
        return
      }
    }
    setShowMenu(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!showMenu) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlightIdx(i => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlightIdx(i => Math.max(i - 1, 0))
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      const choice = filtered[highlightIdx]
      if (choice) {
        e.preventDefault()
        insert(choice.name)
      }
    } else if (e.key === 'Escape') {
      setShowMenu(false)
    }
  }

  return (
    <div ref={wrapperRef} className="relative">
      <input
        ref={ref}
        id={id}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        required={required}
        aria-required={ariaRequired && !required ? true : undefined}
        aria-invalid={invalid ? true : undefined}
        aria-label={ariaLabel}
        aria-describedby={describedBy}
        type={type}
        inputMode={inputMode}
        // The form's own control style, not the shared Input: the two sat side
        // by side at different heights and borders (LIVE-30).
        className={cn(TEXT_INPUT_CLASS, className)}
        {...(combobox
          ? {
              role: 'combobox',
              'aria-expanded': open,
              'aria-haspopup': 'listbox' as const,
              'aria-autocomplete': 'list' as const,
              'aria-controls': listboxId,
              'aria-activedescendant': open ? `${listboxId}-opt-${highlightIdx}` : undefined,
            }
          : {})}
      />
      <SuggestionListbox
        id={listboxId}
        open={open}
        anchorRef={wrapperRef}
        onDismiss={() => setShowMenu(false)}
        suggestions={filtered}
        highlightIdx={highlightIdx}
        onPick={insert}
      />
    </div>
  )
}
