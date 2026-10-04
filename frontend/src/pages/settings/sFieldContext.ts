import { createContext, useContext } from 'react'

/**
 * The id of the hint an enclosing `SField` renders under its label, for the one
 * control that describes itself with it.
 *
 * The event-type forms' label and hint were plain text beside their control, so
 * Name, Regex, Min and the rest were announced as an unlabeled "edit text" and
 * their hints were never read at all. `SField` now renders a real
 * `<label>` through the settings kit's control-id slot (field-control-id.ts);
 * this carries the other half, the hint, to `aria-describedby`.
 *
 * Its own module because a file that exports components may not also export
 * hooks without breaking fast refresh.
 */
export const SFieldHintContext = createContext<string | undefined>(undefined)

/** The enclosing `SField`'s hint id, or undefined when it has no hint. */
export function useSFieldHintId(): string | undefined {
  return useContext(SFieldHintContext)
}

/** Joins describedby ids, dropping the empty ones; undefined when none remain. */
export function describedByIds(...ids: (string | undefined | false)[]): string | undefined {
  const joined = ids.filter(Boolean).join(' ')
  return joined || undefined
}
