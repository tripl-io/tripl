import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { relaxedToJson } from './jsonRelaxed'
import { formatJsonTemplate, templateJsonError, validateJsonWithVars } from './jsonTemplate'
import { JsonTemplateGrid } from './JsonTemplateGrid'
import { parseTemplateRows } from './jsonTemplateGrid'
import { SuggestionListbox } from './VariableInput'
import { filterVariableSuggestions, type VariableSuggestion } from './variableSuggestions'
import { useEvDescribedBy } from './evFieldContext'
import { EV_INPUT_CLASS } from './eventFormLayout'

/** The text the box shows for a stored value: re-indented where it parses. */
function displayJson(value: string): string {
  return value ? formatJsonTemplate(value) ?? value : ''
}

export function JsonEditor({
  id,
  value,
  onChange,
  required,
  invalid = false,
  variables = [],
  defaultMode = 'grid',
}: {
  id?: string
  value: string
  onChange: (v: string) => void
  /** Announced (`aria-required`); the form validates it, not the browser. */
  required?: boolean
  /** Flagged by the form (e.g. an empty required row) on top of the JSON check. */
  invalid?: boolean
  variables?: VariableSuggestion[]
  /** Which view opens first when the value fits the grid. */
  defaultMode?: 'grid' | 'json'
}) {
  const uid = useId()
  const listboxId = `json-var-listbox-${uid}`
  const errorId = `json-error-${uid}`
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  // Validated on mount, not only on the first keystroke: a stored value can be
  // invalid — seven backend paths write a field value without going through
  // `_normalize_json_template_value` — and an untouched field used to render
  // aria-invalid="false" over text the server would refuse.
  const [error, setError] = useState<string | null>(() => validateJsonWithVars(value))
  const [showMenu, setShowMenu] = useState(false)
  const [filter, setFilter] = useState('')
  const [highlightIdx, setHighlightIdx] = useState(0)
  const [insertPos, setInsertPos] = useState(0)
  // What the last Format repaired, and the text it replaced. Repairing is a
  // guess about intent, so it is always both reported and undoable.
  const [repair, setRepair] = useState<{ fixes: string[]; previous: string } | null>(null)
  // The server stores JSON as a single canonical line, so a stored value —
  // templated or not — arrives unbroken. Re-indent it on the way in, or every
  // edit session starts with the whole payload on line one.
  const [raw, setRaw] = useState(() => displayJson(value))
  // The grid is the editor for the common case — an object of keys holding
  // property references and literals (F23) — and "Edit JSON" the way to
  // anything else. A value the grid cannot show opens as JSON.
  // Once open, the grid stays open while it is the one writing: its own
  // half-typed rows (two empty keys) need not parse back into rows.
  const gridFits = parseTemplateRows(raw) !== null
  const [mode, setMode] = useState<'grid' | 'json'>(() =>
    defaultMode === 'grid' && parseTemplateRows(value) !== null ? 'grid' : 'json',
  )
  const showGrid = mode === 'grid'
  // The value this editor last saw from its parent. `raw` used to be read from
  // `value` once and never again, so a reset from outside — "Hand back to
  // scans" clearing the field — changed state the box never showed, and the
  // next keystroke wrote the old payload straight back.
  // Adjust-during-render with an equality guard, this repo's idiom for state
  // that follows a prop (see ProjectAlertingTab.tsx). Only a CHANGE of `value`
  // is considered, and not one that merely echoes what this editor emitted:
  // every emit is `raw` itself, or '' for a whitespace-only box.
  const [seenValue, setSeenValue] = useState(value)
  if (value !== seenValue) {
    setSeenValue(value)
    const echo = value === raw || (value === '' && raw.trim() === '')
    if (!echo) {
      if (parseTemplateRows(value) === null) setMode('json')
      setRaw(displayJson(value))
      setError(validateJsonWithVars(value))
      setRepair(null)
      setShowMenu(false)
    }
  }

  const filtered = useMemo(
    () => filterVariableSuggestions(variables, filter),
    [variables, filter],
  )
  const menuOpen = showMenu && filtered.length > 0
  const describedBy = useEvDescribedBy(error ? errorId : undefined)

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

  const insertVar = useCallback((varName: string) => {
    const before = raw.slice(0, insertPos)
    const after = raw.slice(textareaRef.current?.selectionEnd ?? insertPos)
    const dollarIdx = before.lastIndexOf('$')
    const newValue = before.slice(0, dollarIdx) + '${' + varName + '}' + after
    setRaw(newValue)
    setRepair(null)
    const err = validateJsonWithVars(newValue)
    onChange(newValue)
    setError(err)
    setShowMenu(false)
    setTimeout(() => {
      const pos = dollarIdx + varName.length + 3
      textareaRef.current?.setSelectionRange(pos, pos)
      textareaRef.current?.focus()
    }, 0)
  }, [raw, insertPos, onChange])

  const handleChange = (v: string) => {
    const cursor = textareaRef.current?.selectionStart ?? v.length
    setRaw(v)
    setRepair(null)
    if (!v.trim()) {
      onChange('')
      setError(null)
      setShowMenu(false)
      return
    }
    const err = validateJsonWithVars(v)
    onChange(v)
    setError(err)

    if (variables.length > 0) {
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
        insertVar(choice.name)
      }
    } else if (e.key === 'Escape') {
      setShowMenu(false)
    }
  }

  const apply = (next: string) => {
    setRaw(next)
    onChange(next)
    setError(validateJsonWithVars(next))
  }

  // Format used to fail silently on anything it could not parse, which read as
  // a dead button. Try strict first — already-valid JSON is only re-indented,
  // never reinterpreted — then the tolerant reader, and failing both, say what
  // is wrong.
  const handleFormat = () => {
    if (!raw.trim()) return

    const strict = formatJsonTemplate(raw)
    if (strict !== null) {
      setRepair(null)
      apply(strict)
      return
    }

    // A malformed ${token} is a mistake to report, not to repair.
    const templateError = templateJsonError(raw)
    if (!templateError) {
      const relaxed = relaxedToJson(raw, variables.map(v => v.name))
      const formatted = relaxed && formatJsonTemplate(relaxed.text)
      if (relaxed && formatted && validateJsonWithVars(formatted) === null) {
        setRepair({ fixes: relaxed.fixes, previous: raw })
        apply(formatted)
        return
      }
    }

    setError(templateError ?? validateJsonWithVars(raw))
  }

  // The grid writes the whole template on each edit; the text is still the model.
  const handleGridChange = (next: string) => {
    setRaw(next)
    setRepair(null)
    onChange(next)
    setError(validateJsonWithVars(next))
  }

  const handleUndoRepair = () => {
    if (!repair) return
    const previous = repair.previous
    setRepair(null)
    apply(previous)
  }

  return (
    <div className="space-y-1">
      {/* A small toolbar above the box: Format used to sit alone under
          the box's right edge and read as stray text. Not an overlay either —
          that covered the first line of every payload wider than the box. */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-caption text-fg-tertiary">
          {showGrid ? 'Keys and values' : 'JSON'} · <span className="mono">{'${properties}'}</span> allowed
        </span>
        <div className="flex shrink-0 items-center gap-1">
          {!showGrid && (
            <Button type="button" variant="ghost" size="xs" onClick={handleFormat}>
              Format
            </Button>
          )}
          {showGrid ? (
            <Button type="button" variant="ghost" size="xs" onClick={() => setMode('json')}>
              Edit JSON
            </Button>
          ) : (
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={!gridFits}
              title={gridFits ? undefined : 'The grid edits a JSON object; this value is edited as JSON.'}
              onClick={() => setMode('grid')}
            >
              Edit as grid
            </Button>
          )}
        </div>
      </div>
      {showGrid ? (
        <JsonTemplateGrid
          id={id}
          value={raw}
          onChange={handleGridChange}
          variables={variables}
          invalid={!!error || invalid}
        />
      ) : (
      <div ref={wrapperRef} className="relative">
        {/* The form's one control style, not the shared Textarea: its border,
            background and focus colour differed from every neighbouring input
            (the last LIVE-30 holdout). `aria-invalid` draws the danger
            edge through INPUT_CLASS. */}
        <textarea
          ref={textareaRef}
          id={id}
          value={raw}
          onChange={e => handleChange(e.target.value)}
          onKeyDown={handleKeyDown}
          className={cn(EV_INPUT_CLASS, 'block min-h-[96px] py-2 font-mono leading-[1.5]')}
          rows={4}
          placeholder='{ "key": "value" }'
          aria-required={required || undefined}
          spellCheck={false}
          role="combobox"
          aria-invalid={error || invalid ? 'true' : 'false'}
          aria-expanded={menuOpen}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-describedby={describedBy}
          aria-activedescendant={menuOpen ? `${listboxId}-opt-${highlightIdx}` : undefined}
        />
        <SuggestionListbox
          id={listboxId}
          open={menuOpen}
          anchorRef={wrapperRef}
          onDismiss={() => setShowMenu(false)}
          suggestions={filtered}
          highlightIdx={highlightIdx}
          onPick={insertVar}
        />
      </div>
      )}
      <p id={errorId} className="min-w-0 text-body-sm text-destructive empty:hidden">{error}</p>
      {repair && (
        <div className="flex items-start justify-between gap-2" aria-live="polite">
          <p className="min-w-0 text-body-sm text-fg-tertiary">
            Format {repair.fixes.join(', ')}.
          </p>
          <Button type="button" variant="ghost" size="xs" onClick={handleUndoRepair} className="shrink-0">
            Undo
          </Button>
        </div>
      )}
    </div>
  )
}
