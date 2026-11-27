/**
 * Links from a place a check run found (a file and line) to the repository,
 * through the project's link template (Settings › Project › Check runs).
 *
 * The backend refuses a template that is not http(s) or whose host is not
 * written out, so no uploaded value can move a link to another site. This side
 * still escapes every value, and gives up (plain text) on anything that does
 * not come out as an http(s) URL.
 */

/** What a template may name; the backend's `CODE_URL_PLACEHOLDERS`. */
export const CODE_URL_PLACEHOLDERS = [
  'repository_url',
  'repository',
  'ref',
  'commit',
  'path',
  'line',
] as const

export type CodeUrlPlaceholder = (typeof CODE_URL_PLACEHOLDERS)[number]

/** One place, with the run it was found in. */
export interface CodePlace {
  repositoryUrl?: string | null
  repository: string
  ref: string
  commit: string
  path: string
  line: number
}

/**
 * A slash-separated value with each segment percent-encoded and the slashes
 * kept, or null when a segment is `.` or `..`, which a browser would resolve
 * and so walk the link out of the place it names.
 */
function encodeSegments(value: string): string | null {
  const segments = value.split('/')
  if (segments.some((segment) => segment === '.' || segment === '..')) return null
  return segments.map(encodeURIComponent).join('/')
}

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return (url.protocol === 'http:' || url.protocol === 'https:') && url.host !== ''
  } catch {
    return false
  }
}

/**
 * The template filled in for one place, or null when there is no template, a
 * placeholder has no value (`{repository_url}` for a run that sent none), or
 * the result is not an http(s) URL.
 */
export function codeUrl(template: string | null | undefined, place: CodePlace): string | null {
  if (!template) return null
  const repositoryUrl = place.repositoryUrl?.replace(/\/+$/, '') ?? null
  const values: Record<CodeUrlPlaceholder, string | null> = {
    repository_url: repositoryUrl && isHttpUrl(repositoryUrl) ? repositoryUrl : null,
    repository: encodeSegments(place.repository),
    ref: encodeSegments(place.ref),
    commit: encodeURIComponent(place.commit),
    path: encodeSegments(place.path.replace(/^\.\//, '')),
    line: Number.isInteger(place.line) && place.line >= 0 ? String(place.line) : null,
  }
  let complete = true
  const url = template.replace(/\{([^{}]*)\}/g, (_match, name: string) => {
    const value = (CODE_URL_PLACEHOLDERS as readonly string[]).includes(name)
      ? values[name as CodeUrlPlaceholder]
      : null
    if (value === null) complete = false
    return value ?? ''
  })
  return complete && isHttpUrl(url) ? url : null
}
