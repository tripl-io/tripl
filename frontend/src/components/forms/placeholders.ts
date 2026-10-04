/**
 * Placeholders that cannot be mistaken for a value.
 *
 * A placeholder of exactly what a real value looks like (`created_at`,
 * `localhost`, `default`, `%`, a runnable query) read as prefilled, so
 * "Timestamp column is required" appeared next to a box that seemed to say
 * `created_at`. A placeholder is either an instruction ("Pick a column") or a
 * marked example, built here. Colour comes from INPUT_PLACEHOLDER_CLASS
 * (`--fg-faint`) in components/settings/input-style.
 */

/** "e.g. created_at" / "e.g. %, ms, $" for a single-line example. */
export function examplePlaceholder(...examples: string[]): string {
  return `e.g. ${examples.join(', ')}`
}

/**
 * A SQL editor placeholder that is visibly a comment, never a query: every
 * line starts with `-- `. The first line says what to write; the example
 * follows, commented out, so it cannot be read as code already in the editor.
 * Keep the example in the target warehouse's dialect (ClickHouse in the demo).
 *
 *   sqlPlaceholder('Return a time column and a numeric value, e.g.',
 *     'SELECT toStartOfHour(ts) AS bucket, count() AS value FROM events GROUP BY bucket')
 */
export function sqlPlaceholder(instruction: string, example?: string): string {
  const lines = [instruction, ...(example ? example.split('\n') : [])]
  return lines.map(line => (line === '' ? '--' : `-- ${line}`)).join('\n')
}
