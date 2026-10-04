/**
 * Layout primitives shared by the event authoring pages.
 *
 * Extracted verbatim from `EventForm.tsx`, where they were private, when a
 * second authoring surface appeared: the bulk page must look like the single
 * one, and two copies of a card and a labelled row is how two surfaces that are
 * meant to be the same screen quietly stop being it.
 */
import { useId, type ComponentProps, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { Field } from '@/components/settings/kit'
import { INPUT_CLASS } from '@/components/settings/input-style'
import { cn } from '@/lib/utils'
import { EvFieldContext, useEvDescribedBy } from './evFieldContext'

// One control style for every section of the form. Details used this class while
// Field values and Meta fields rendered the shared `Input` (a different height
// and border), so one card read as two forms. `:disabled` also matches
// a control inside a disabled fieldset, which is how the read-only view and the
// locked Event type select now look locked rather than live.
// INPUT_CLASS gives an `aria-invalid` control the danger edge and halo and the
// placeholder its faint colour; 16px text below md keeps iOS Safari from
// zooming into every field it focuses.
// eslint-disable-next-line react-refresh/only-export-components -- a class string, not state
export const EV_INPUT_CLASS = cn(
  'w-full rounded-control border bg-[var(--bg)] px-[11px] text-base md:text-body-sm text-[var(--fg)] outline-none focus:border-[var(--accent)] disabled:cursor-not-allowed disabled:bg-[var(--surface-hover)] disabled:text-[var(--fg-muted)]',
  INPUT_CLASS,
)
export const SELECT_CLASS = `${EV_INPUT_CLASS} h-8 cursor-pointer appearance-none pr-[30px]`
export const TEXT_INPUT_CLASS = `${EV_INPUT_CLASS} h-8`

/**
 * The two widths a control takes on this form. Widths used to be picked per
 * field (Title 340px, Owner 230px, a boolean 160px, Description the whole row)
 * for no reason a reader could see; now free text takes the row and a
 * choice from a list takes half of a desktop row.
 */
export type EvControlWidth = 'full' | 'half'
export const EV_FULL_WIDTH_CLASS = 'w-full'
export const EV_HALF_WIDTH_CLASS = 'max-w-[240px]'
const HALF_WIDTH_PX = 240

export function SurfCard({
  title,
  subtitle,
  children,
}: {
  title: string
  /** One line under the title; may carry a link to where the thing is defined. */
  subtitle?: ReactNode
  children: ReactNode
}) {
  // The kit Panel's geometry: 10px radius, a 16px header gutter that
  // lines up with the kit Field rows under it, a 12.5px h2 title and an
  // 11.5px subtitle — not a 12px-radius card with its own 14px title.
  return (
    <div
      className="mb-[18px] overflow-hidden rounded-card border bg-surface border-border"
    >
      <div className="border-b px-4 py-3 border-border-subtle">
        <h2 className="m-0 text-body-sm font-semibold">{title}</h2>
        {subtitle && (
          <div className="mt-0.5 text-caption text-fg-tertiary">
            {subtitle}
          </div>
        )}
      </div>
      {children}
    </div>
  )
}

export function EvField({
  label,
  hint,
  htmlFor,
  required,
  last,
  notes,
  children,
}: {
  label: string
  hint?: ReactNode
  htmlFor?: string
  required?: boolean
  last?: boolean
  /** Notices under the control (a warning, what saving will do). Rendered
   *  after it and tied to it through `aria-describedby`. */
  notes?: ReactNode
  children: ReactNode
}) {
  const uid = useId()
  const hintId = hint ? `${uid}-hint` : undefined
  const notesId = notes ? `${uid}-notes` : undefined
  const describedBy = [hintId, notesId].filter(Boolean).join(' ') || undefined
  // The kit Field row: one implementation of the caption, the phone
  // stacking and the decorative required star — the control itself carries
  // `required` / `aria-required`, which is what a screen reader announces.
  // This keeps the form's 200px caption column and hands the hint and
  // notes ids to the Ev* controls through EvFieldContext. A row naming no
  // control is labelled as a group rather than by a `<label>` pointing nowhere.
  return (
    <Field
      label={label}
      hint={hint ? <span id={hintId}>{hint}</span> : undefined}
      htmlFor={htmlFor ?? false}
      required={required}
      last={last}
      labelWidth={200}
    >
      <EvFieldContext.Provider value={{ describedBy }}>
        {children}
        {notes && <div id={notesId}>{notes}</div>}
      </EvFieldContext.Provider>
    </Field>
  )
}

/** A text input in the form's one style, described by its row. */
export function EvInput({
  width = 'full',
  className,
  ...props
}: ComponentProps<'input'> & { width?: EvControlWidth }) {
  const describedBy = useEvDescribedBy(props['aria-describedby'])
  return (
    <input
      {...props}
      aria-describedby={describedBy}
      className={cn(TEXT_INPUT_CLASS, width === 'full' ? EV_FULL_WIDTH_CLASS : EV_HALF_WIDTH_CLASS, className)}
    />
  )
}

/** A textarea in the form's one style, described by its row. */
export function EvTextarea({ className, ...props }: ComponentProps<'textarea'>) {
  const describedBy = useEvDescribedBy(props['aria-describedby'])
  return (
    <textarea
      {...props}
      aria-describedby={describedBy}
      className={cn(EV_INPUT_CLASS, EV_FULL_WIDTH_CLASS, 'min-h-[60px] py-2 leading-[1.5]', className)}
    />
  )
}

export function SelectControl({
  id,
  value,
  onChange,
  disabled,
  required,
  ariaRequired,
  width = 'half',
  children,
  ...aria
}: {
  id?: string
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  required?: boolean
  /** Announced as required without the browser enforcing it. */
  ariaRequired?: boolean
  width?: EvControlWidth
  children: ReactNode
  /** From `invalidAria(id, error)`: the danger outline and the message id. */
  'aria-invalid'?: boolean
  'aria-describedby'?: string
}) {
  const describedBy = useEvDescribedBy(aria['aria-describedby'])
  return (
    <div className="relative" style={{ maxWidth: width === 'half' ? HALF_WIDTH_PX : undefined }}>
      <select
        id={id}
        value={value}
        onChange={e => onChange(e.target.value)}
        disabled={disabled}
        required={required}
        aria-required={ariaRequired && !required ? true : undefined}
        aria-invalid={aria['aria-invalid'] || undefined}
        aria-describedby={describedBy}
        className={SELECT_CLASS}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-[10px] top-1/2 -translate-y-1/2 text-fg-tertiary"
        size={14}
        aria-hidden="true"
      />
    </div>
  )
}
