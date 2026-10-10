/** The line on the bulk page's action bar, beside Create. */
export interface BulkFormStatus {
  text: string
  tone: 'muted' | 'danger'
}

/**
 * Why Create is held, or nothing once it is not.
 *
 * A step the reader has not taken yet — no type picked, nothing pasted, the
 * names still being checked — is muted, like the single-event form, which
 * only turns red after a submit: the page used to open on a red "Pick an event
 * type" before anything had been touched. Red is for what is wrong with what
 * was given: a type a paste cannot fill, or a paste with no line to create.
 */
export function bulkFormStatus(state: {
  hasEventTypes: boolean
  typeChosen: boolean
  cannotPaste: boolean
  lineCount: number
  checking: boolean
  readyCount: number
}): BulkFormStatus | null {
  if (!state.hasEventTypes) return { text: 'Create an event type first', tone: 'muted' }
  if (!state.typeChosen) return { text: 'Pick an event type, then paste one event per line', tone: 'muted' }
  if (state.cannotPaste) return { text: 'This event type cannot be filled from a pasted list', tone: 'danger' }
  if (state.lineCount === 0) return { text: 'Paste at least one event name', tone: 'muted' }
  // Not a blocker: the probe answers within a second or two.
  if (state.checking) return { text: 'Checking the names…', tone: 'muted' }
  if (state.readyCount === 0) return { text: 'No line can be created', tone: 'danger' }
  return null
}
