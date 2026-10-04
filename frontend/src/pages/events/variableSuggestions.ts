import { suggestionMatches } from './utils'

/** Structural subset of Variable — full Variable objects satisfy it. */
export interface VariableSuggestion {
  name: string
  description?: string
  bindings?: string[]
  allowed_values?: string[]
}

/**
 * How many suggestions a `$` opens at once. A project with 200 variables used to
 * render a 200-row list running far past the viewport and over Save;
 * typing narrows it, so the tail is one keystroke away.
 */
export const MAX_VARIABLE_SUGGESTIONS = 50

/** The variables matching what follows the `$`, capped for the dropdown. */
export function filterVariableSuggestions(
  variables: VariableSuggestion[],
  filter: string,
): VariableSuggestion[] {
  const matches: VariableSuggestion[] = []
  for (const variable of variables) {
    if (!suggestionMatches(variable, filter)) continue
    matches.push(variable)
    if (matches.length === MAX_VARIABLE_SUGGESTIONS) break
  }
  return matches
}
