import { useId, useRef, useState } from 'react'
import { X } from 'lucide-react'

const DEFAULT_INVALID_MESSAGE = 'That value is not accepted here.'
const DUPLICATE_MESSAGE = 'Already added.'

export interface ChipListInputProps {
  values: string[]
  onChange: (next: string[]) => void
  placeholder: string
  ariaLabel: string
  validate?: (value: string) => boolean
  /** Shown when `validate` rejects the draft. */
  invalidMessage?: string
  inputId?: string
}

/**
 * A list of short values entered one at a time — the interaction Tags already
 * had, and the one the analyst named for several Jira keys on one event.
 *
 * Values are kept EXACTLY as typed. Tags lower-case theirs, which is right for
 * a free-form label and wrong for everything else here: it would turn PROJ-4770
 * into proj-4770, and a documented value into a different string.
 */
export function ChipListInput({
  values,
  onChange,
  placeholder,
  ariaLabel,
  validate,
  invalidMessage = DEFAULT_INVALID_MESSAGE,
  inputId,
}: ChipListInputProps) {
  const [draft, setDraft] = useState('')
  // The message on screen, or null. One slot for both reasons a draft is not
  // added, so a screen reader hears exactly the one that applies (DS-18).
  const [problem, setProblem] = useState<string | null>(null)
  const errorId = `${useId()}-error`
  const rootRef = useRef<HTMLDivElement>(null)

  /**
   * Adds the draft. `quiet` is for focus moving to one of this control's own
   * chip remove buttons: that is not a request to validate half-typed text, so
   * a rejected draft is simply left in the box. Any other blur (Tab onward, a
   * click on the form's Save) says why the draft was not added — otherwise the
   * form saves without it and the only hint is leftover text in the box.
   */
  const add = (quiet = false) => {
    const value = draft.trim()
    if (!value) return
    if (validate && !validate(value)) {
      if (!quiet) setProblem(invalidMessage)
      return
    }
    if (values.includes(value)) {
      // Said, not silently swallowed: the draft used to vanish with no sign
      // the value was already in the list.
      if (!quiet) setProblem(DUPLICATE_MESSAGE)
      return
    }
    onChange([...values, value])
    setDraft('')
    setProblem(null)
  }

  const invalid = problem !== null
  return (
    <div>
      <div
        ref={rootRef}
        className="flex min-h-8 flex-wrap items-center gap-1 rounded-control border border-input bg-transparent px-2.5 py-1 focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50"
      >
        {values.map(value => (
          // A code value, so the CodeToken look (sunken, square, mono; DS-6),
          // with room for its remove button. On phones the chip is 32px and
          // the remove button a 28px square: an 11px icon in a 22px chip was
          // nearly impossible to hit (AU-39).
          <span
            key={value}
            className="flex items-center gap-1 rounded-sm border border-border-subtle bg-bg-sunken py-0.5 pl-1.5 pr-0.5 font-mono text-caption max-sm:h-8"
          >
            {value}
            <button
              type="button"
              aria-label={`Remove ${value}`}
              // The icon is 12px; the pointer target grows to 24px (WCAG 2.5.8).
              className="hit-target-24 grid place-items-center rounded-sm hover:text-destructive max-sm:size-7"
              onClick={() => onChange(values.filter(v => v !== value))}
            >
              <X className="h-3 w-3" aria-hidden="true" />
            </button>
          </span>
        ))}
        <input
          id={inputId}
          aria-label={ariaLabel}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? errorId : undefined}
          className="h-6 min-w-28 flex-1 bg-transparent text-body outline-none"
          value={draft}
          onChange={e => {
            setDraft(e.target.value)
            setProblem(null)
          }}
          // Enter adds a chip and nothing else: this control is mounted inside
          // forms whose own submit saves something much larger.
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            } else if (e.key === 'Backspace' && draft === '' && values.length > 0) {
              // The conventional token-input gesture: Backspace in an empty
              // box takes back the last chip.
              e.preventDefault()
              onChange(values.slice(0, -1))
            }
          }}
          onBlur={e => {
            const next = e.relatedTarget
            add(next instanceof Node && rootRef.current?.contains(next) === true)
          }}
          placeholder={placeholder}
        />
      </div>
      {invalid && (
        <p id={errorId} role="alert" className="mt-1 text-body-sm text-destructive">
          {problem}
        </p>
      )}
    </div>
  )
}
