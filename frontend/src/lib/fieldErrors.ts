/**
 * Wiring between a settings-style form's field-keyed validation messages and
 * the controls they describe (the metric and fact-table editors). Every message
 * is keyed by the DOM id of its control; the inline message under it (rendered
 * by the kit `Field`, `error` prop) gets `${id}-error`, and the control points at it. Before this
 * the messages were plain paragraphs no control referenced, so a screen reader
 * heard neither that a field was invalid nor why.
 */

export type FieldErrors = Readonly<Record<string, string>>

export function fieldErrorId(fieldId: string): string {
  return `${fieldId}-error`
}

/** Props that link a control to its inline message, or nothing when it is valid. */
export function errorAria(
  errors: FieldErrors | undefined,
  fieldId: string,
): { 'aria-invalid'?: boolean; 'aria-describedby'?: string } {
  if (!errors?.[fieldId]) return {}
  return { 'aria-invalid': true, 'aria-describedby': fieldErrorId(fieldId) }
}

const FOCUSABLE = 'input, select, textarea, button, [contenteditable="true"], [tabindex]'

/**
 * Scroll a field into view and move keyboard focus INTO it. The SQL editor puts
 * its id on CodeMirror's contenteditable itself (matched by `[contenteditable]`
 * below). Other wrapped controls may still carry the id on a non-focusable
 * wrapper, so such a target hands focus to its first focusable descendant;
 * calling `.focus()` on a plain div does nothing and would leave focus on Save.
 * scrollIntoView is guarded because jsdom does not implement it.
 */
export function focusField(fieldId: string): void {
  const el = document.getElementById(fieldId)
  if (!el) return
  if (typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  const target = el.matches(FOCUSABLE) ? el : el.querySelector<HTMLElement>(FOCUSABLE)
  ;(target ?? el).focus()
}
