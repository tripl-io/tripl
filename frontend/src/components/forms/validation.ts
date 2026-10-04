/**
 * The one validation pattern for authoring forms.
 *
 *   1. `<form noValidate onSubmit={submit}>`: no native `required`/`pattern`
 *      bubbles. Keep `required` off the inputs (or keep it only as
 *      `aria-required`), and put the format rule into the message instead of
 *      a `pattern` ("Use lowercase letters, digits and _; start with a letter.").
 *   2. Compute errors as a `Record<fieldId, message>` on every render; show
 *      them once the user has pressed Save (a `submitted` flag) or left the
 *      field, not while they are still typing into an empty form.
 *   3. Each control gets `{...invalidAria(id, shownError)}`; the message sits
 *      under it in `<FieldError inputId={id} message={shownError} />` (or the
 *      kit `Field`'s `error` prop). `aria-invalid` draws the danger outline.
 *   4. On a blocked submit, name what is missing next to Save with
 *      `missingSummary(labels)` in `--danger`, and `focusFirstInvalid(form)`.
 */
import { fieldErrorId, focusField } from '@/lib/fieldErrors'

/** The message under an empty required control. */
export const REQUIRED_MESSAGE = 'Required'

/**
 * `aria-invalid` + `aria-describedby` for a control whose message
 * {@link FieldError} renders under it, or nothing while it is valid. The
 * single-field twin of `errorAria` in lib/fieldErrors.
 */
export function invalidAria(
  fieldId: string,
  message: unknown,
): { 'aria-invalid'?: true; 'aria-describedby'?: string } {
  if (message === undefined || message === null || message === false || message === '') return {}
  return { 'aria-invalid': true, 'aria-describedby': fieldErrorId(fieldId) }
}

/**
 * The summary next to a blocked Save: "Fill in: Name, Screen name". Null when
 * nothing is missing, so it can gate the line directly.
 */
export function missingSummary(labels: readonly string[]): string | null {
  if (labels.length === 0) return null
  return `Fill in: ${labels.join(', ')}`
}

/**
 * The count form of the same summary, for a long form whose problems are not
 * all "missing": "1 field needs attention" / "3 fields need attention".
 */
export function attentionSummary(count: number): string | null {
  if (count <= 0) return null
  return count === 1 ? '1 field needs attention' : `${count} fields need attention`
}

/**
 * Scroll to and focus the first control marked `aria-invalid="true"` inside
 * `root`, in document order. Call it after the render that shows the errors
 * (e.g. in a `requestAnimationFrame` or an effect keyed on the submit count).
 * Returns whether one was found.
 */
export function focusFirstInvalid(root: ParentNode = document): boolean {
  const el = root.querySelector<HTMLElement>('[aria-invalid="true"]')
  if (!el) return false
  if (el.id) {
    focusField(el.id)
  } else {
    if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.focus()
  }
  return true
}
