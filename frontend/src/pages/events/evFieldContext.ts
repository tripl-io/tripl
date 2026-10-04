import { createContext, useContext } from 'react'

/**
 * What an `EvField` row tells the control inside it: the ids of the hint beside
 * the label and of the notes under the control, joined for `aria-describedby`.
 *
 * The hints on this form are consequential — "Saving this stops scans from
 * updating the field", "An event already answers to this name" — and they were
 * visual only, so a screen-reader user tabbing into the box heard the label and
 * nothing of what typing into it would do. The controls read this
 * rather than taking a prop because a row's control is often nested a few
 * components deep (a coach mark, a field-type switch).
 */
export const EvFieldContext = createContext<{ describedBy?: string }>({})

/** The row's description ids, merged with any the control adds of its own. */
export function useEvDescribedBy(...own: (string | undefined | null | false)[]): string | undefined {
  const { describedBy } = useContext(EvFieldContext)
  const ids = [describedBy, ...own].filter((id): id is string => !!id)
  return ids.length > 0 ? ids.join(' ') : undefined
}
