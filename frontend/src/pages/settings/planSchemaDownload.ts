/**
 * The "Export JSON Schema" download on the plan history page (GH #262, F09).
 * Kept out of the component so the file name and the blob handling are
 * testable without rendering the page.
 */

/** `tripl-plan-schema-<slug>-<branch>-2026-09-27.json` (`main` for main). */
export function planSchemaFilename(
  slug: string,
  branchName: string | null,
  now = new Date(),
): string {
  // A branch name may hold "/" (feature/checkout); a file name may not.
  const branch = branchName ? `-${branchName.replace(/[^A-Za-z0-9._-]+/g, '-')}` : ''
  return `tripl-plan-schema-${slug}${branch}-${now.toISOString().slice(0, 10)}.json`
}

export function downloadJson(filename: string, data: unknown): void {
  const text = `${JSON.stringify(data, null, 2)}\n`
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Deferred for the same reason as the events CSV (EVT-41): some browsers
  // start the download after click() returns.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
