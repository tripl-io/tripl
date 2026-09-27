import { Link } from 'react-router-dom'
import { AlertTriangle, Lightbulb } from 'lucide-react'
import type { DuplicateCheckResult, DuplicateMatch } from '@/types'
import {
  duplicateEventPath,
  duplicateSummary,
  formatDuplicateScore,
  lintIssues,
  suggestedName,
  visibleDuplicates,
} from './duplicateHints'

const LINK_CLASS = 'underline underline-offset-2'
const ACTION_CLASS =
  'underline underline-offset-2 hover:text-[var(--fg)] disabled:cursor-not-allowed disabled:opacity-60'

const NO_EXCLUDED: ReadonlySet<string> = new Set()

/**
 * What the catalog says about a would-be event (F12, #265): "Looks like
 * <Name> (94%) — Open · Mark as replacement", then the naming-convention
 * hints with "Use suggested name". Advisory throughout — nothing here blocks
 * a save; the name must stay whatever the app really sends.
 *
 * `compact` is the one-line form for a table row (the bulk preview): the best
 * match and the first hint, no replacement action.
 */
export function DuplicateHints({
  slug,
  name,
  result,
  exclude = NO_EXCLUDED,
  onUseSuggestion,
  onMarkReplacement,
  replacement = null,
  onClearReplacement,
  compact = false,
  disabled = false,
}: {
  slug: string
  /** The name on screen, so a suggestion equal to it is not offered. */
  name: string
  result: DuplicateCheckResult | undefined
  /** Event ids the caller already reports another way (an exact namesake). */
  exclude?: ReadonlySet<string>
  /** Omitted where the name cannot be edited here (a scan rule writes it). */
  onUseSuggestion?: (suggestion: string) => void
  /** Omitted where nothing can be marked (a viewer, the bulk preview). */
  onMarkReplacement?: (match: DuplicateMatch) => void
  /** The match this event has been marked to replace. */
  replacement?: Pick<DuplicateMatch, 'event_id' | 'name'> | null
  onClearReplacement?: () => void
  compact?: boolean
  disabled?: boolean
}) {
  const matches = visibleDuplicates(result, exclude, compact ? 1 : 3)
  const issues = lintIssues(result)
  const suggestion = suggestedName(result, name)
  const shownIssues = compact ? issues.slice(0, 1) : issues

  if (matches.length === 0 && shownIssues.length === 0 && !suggestion && !replacement) return null

  return (
    <div className={compact ? 'mt-0.5 space-y-0.5 text-caption' : 'mt-1 space-y-1 text-body-sm'} data-testid="duplicate-hints">
      {replacement ? (
        <p className="m-0 text-fg-secondary">
          Replaces “{replacement.name}”: once this event is created, that one is deprecated with
          this one as its successor.{' '}
          {onClearReplacement && (
            <button type="button" className={ACTION_CLASS} onClick={onClearReplacement} disabled={disabled}>
              Undo
            </button>
          )}
        </p>
      ) : (
        matches.map(match => (
          // Plain text: the caller's one DuplicateLiveRegion announces the
          // count, so each row is not a live region of its own.
          <p key={match.event_id} className="m-0 flex items-start gap-1.5 text-warning">
            <AlertTriangle className="mt-[3px] size-3.5 shrink-0" aria-hidden="true" />
            <span>
              Looks like <span className="font-medium">{match.name}</span> (
              {formatDuplicateScore(match.score)})
              {' — '}
              <Link
                to={duplicateEventPath(slug, match.event_id)}
                className={LINK_CLASS}
                aria-label={`Open ${match.name}`}
              >
                Open
              </Link>
              {onMarkReplacement && !compact && (
                <>
                  {' · '}
                  <button
                    type="button"
                    className={ACTION_CLASS}
                    onClick={() => onMarkReplacement(match)}
                    disabled={disabled}
                  >
                    Mark as replacement
                  </button>
                </>
              )}
              {!compact && match.reasons.length > 0 && (
                <span className="text-fg-tertiary"> · {match.reasons.join(', ')}</span>
              )}
            </span>
          </p>
        ))
      )}
      {shownIssues.map(issue => (
        <p key={issue.code} className="m-0 flex items-start gap-1.5 text-fg-tertiary">
          <Lightbulb className="mt-[3px] size-3.5 shrink-0" aria-hidden="true" />
          <span>{issue.message}</span>
        </p>
      ))}
      {suggestion && (
        <p className="m-0 text-fg-tertiary">
          Suggested: <span className="mono text-fg">{suggestion}</span>
          {onUseSuggestion && (
            <>
              {' '}
              <button
                type="button"
                className={ACTION_CLASS}
                onClick={() => onUseSuggestion(suggestion)}
                disabled={disabled}
              >
                Use suggested name
              </button>
            </>
          )}
        </p>
      )}
    </div>
  )
}

/**
 * The one persistent polite live region a form or a table keeps for its
 * duplicate hints: always mounted (a region inserted together with its text is
 * often not announced), its text a summary — "2 possible duplicates" — so the
 * rows themselves stay plain text.
 */
export function DuplicateLiveRegion({ count }: { count: number }) {
  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="duplicate-live-region">
      {duplicateSummary(count)}
    </p>
  )
}
