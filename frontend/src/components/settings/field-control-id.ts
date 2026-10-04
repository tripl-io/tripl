import { createContext, useContext, useId } from 'react'

/**
 * The id an enclosing `Field` offers its content, plus who has taken it.
 *
 * A `Field` renders exactly ONE `<label htmlFor>`, so exactly one control in the
 * row may carry that id — but a row is free to hold several. The metric form's
 * "Filters" row wraps a filter editor that renders two Selects and a TextInput
 * per condition, and handing the same id to all of them puts duplicate ids on
 * focusable elements and leaves `document.getElementById` ambiguous.
 */
export type FieldControlIdSlot = {
  /** The id the enclosing `Field`'s <label> points at. */
  id: string
  /**
   * The row's validation state, for the same one control the label names:
   * the error message's id, whether it is showing, and whether the row is
   * required. Kit controls apply them unless given their own.
   */
  aria?: FieldControlAria
  /**
   * True for the first control that asks, false for every other one.
   *
   * Keyed by the caller's own React id rather than by call order, so a control
   * re-rendering on its own — or StrictMode invoking the tree twice — re-asks
   * without the id migrating to the next control in the row. `Field` mints a
   * fresh slot on each of its own renders, so a row whose first control is
   * removed hands the id to the next one instead of stranding it.
   */
  claim: (consumerId: string) => boolean
}

/** What a `Field` tells the control it labels about its validation state. */
export type FieldControlAria = {
  describedBy?: string
  invalid?: boolean
  required?: boolean
}

export function createFieldControlIdSlot(id: string, aria?: FieldControlAria): FieldControlIdSlot {
  let owner: string | null = null
  return {
    id,
    aria,
    claim: (consumerId: string) => {
      if (owner === null) owner = consumerId
      return owner === consumerId
    },
  }
}

/** Set by `Field` to the slot holding the id its `<label htmlFor>` points at. */
export const FieldControlIdContext = createContext<FieldControlIdSlot | null>(null)

/**
 * The id the enclosing `Field`'s <label> points at, for the one control that
 * claims it.
 *
 * `Field` renders its children raw, so before this the generated `htmlFor`
 * addressed an element that did not exist unless the caller passed `htmlFor`
 * AND repeated the same id on its own control — almost nobody did, and 10 of 14
 * inputs on /settings/instance/ai shipped with no accessible name.
 * The kit controls adopt it automatically; a non-kit control placed inside a
 * `Field` should read it here and set it as its own `id`.
 *
 * It lives beside kit.tsx rather than inside it because a module that exports
 * components may not also export hooks without breaking fast refresh.
 */
export function useFieldControlId(explicitId?: string): string | undefined {
  return useFieldControl(explicitId).id
}

const NO_ARIA: FieldControlAria = {}

/**
 * {@link useFieldControlId}, plus the enclosing `Field`'s validation state for
 * the control that owns its label: `aria-describedby` for the error, and
 * `aria-invalid` / `aria-required`. A control that passes its own `id`
 * owns the row's state only when that id is the one the label points at.
 */
export function useFieldControl(explicitId?: string): { id: string | undefined; aria: FieldControlAria } {
  const slot = useContext(FieldControlIdContext)
  // Unconditional — this is the key the slot remembers its owner by.
  const consumerId = useId()
  if (explicitId) {
    return { id: explicitId, aria: slot && slot.id === explicitId ? (slot.aria ?? NO_ARIA) : NO_ARIA }
  }
  if (!slot) return { id: undefined, aria: NO_ARIA }
  return slot.claim(consumerId) ? { id: slot.id, aria: slot.aria ?? NO_ARIA } : { id: undefined, aria: NO_ARIA }
}
