import type { DuplicateCheckResult, DuplicateMatch, NameLintIssue } from '@/types'
import { countOf } from '@/lib/plural'

/** "94%": a score as the warning reads it. Floored, so 0.879 never reads as
 *  the 88% threshold it did not reach. */
export function formatDuplicateScore(score: number): string {
  const clamped = Math.min(1, Math.max(0, score))
  return `${Math.floor(clamped * 100)}%`
}

/**
 * The matches worth warning about, in the server's order — it ranks same-type
 * matches ahead of cross-type ones, so re-sorting by score here would undo
 * that. At most `limit`, without the events the caller already names another
 * way (the exact namesake the form reports on its own line, an event this form
 * has just created). Filter and slice only.
 */
export function visibleDuplicates(
  result: DuplicateCheckResult | undefined,
  exclude: ReadonlySet<string> = new Set(),
  limit = 3,
): DuplicateMatch[] {
  if (!result) return []
  return result.duplicates.filter(match => !exclude.has(match.event_id)).slice(0, limit)
}

/**
 * What the one polite live region per form or table says: "2 possible
 * duplicates", or nothing. Summarised so a screen reader hears one sentence
 * when the answer lands, not every row the answer contains.
 */
export function duplicateSummary(count: number): string {
  if (count <= 0) return ''
  return countOf(count, 'possible duplicate', 'possible duplicates')
}

/**
 * The name to offer behind "Use suggested name": the whole-name suggestion
 * when the server made one, else the first issue's. Null when it would not
 * change the name.
 */
export function suggestedName(
  result: DuplicateCheckResult | undefined,
  current: string,
): string | null {
  if (!result) return null
  const candidates = [result.suggestion, ...result.lint.map(issue => issue.suggestion)]
  for (const candidate of candidates) {
    const trimmed = candidate?.trim()
    if (trimmed && trimmed !== current.trim()) return trimmed
  }
  return null
}

/** Lint issues, one per code, in the server's order. */
export function lintIssues(result: DuplicateCheckResult | undefined): NameLintIssue[] {
  if (!result) return []
  const seen = new Set<string>()
  return result.lint.filter(issue => {
    if (seen.has(issue.code)) return false
    seen.add(issue.code)
    return true
  })
}

/** The event's page, where every "Open" in this feature leads. */
export function duplicateEventPath(slug: string, eventId: string): string {
  return `/p/${slug}/monitoring/event/${eventId}`
}
