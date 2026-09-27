import { useContext, useId, useState } from 'react'
import { Link } from 'react-router-dom'

import type { IncidentSummaryBody, IncidentSummaryFact } from '@/api/incidentSummary'
import { ActiveProjectContext } from '@/components/active-project-context'
import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/datetime'
import { cn } from '@/lib/utils'

import { useIncidentSummary } from './useIncidentSummary'

const SMALL_BUTTON = 'h-9 px-3 text-body-sm sm:h-7 sm:px-2 sm:text-caption'

/**
 * A short, cited account of one incident (F14, #267): what broke, the likely
 * cause, a release, similar past verdicts and the discussion, each sentence
 * linked to the numbered fact it rests on.
 *
 * Renders nothing while AI is off, loading its status, or the project is a
 * demo (read from the active project, before any request): no button, no
 * error, nothing to explain.
 *
 * On the inbox card it is a collapsed disclosure and fetches only once opened;
 * the signal page passes `defaultOpen`.
 */
export function IncidentSummary({
  slug,
  correlationGroupId,
  canWrite,
  defaultOpen = false,
  className,
}: {
  slug: string
  correlationGroupId: string
  canWrite: boolean
  defaultOpen?: boolean
  className?: string
}) {
  const [open, setOpen] = useState(defaultOpen)
  const panelId = useId()
  const isDemo = useContext(ActiveProjectContext)?.is_demo === true
  const summary = useIncidentSummary({ slug, correlationGroupId, active: open, isDemo })

  if (isDemo || !summary.aiEnabled || summary.data?.state === 'disabled') return null

  return (
    <div className={cn('mt-2', className)} data-testid="incident-summary">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(value => !value)}
        className="inline-flex min-h-9 items-center text-body-sm underline underline-offset-2 text-fg-tertiary hover:text-foreground sm:min-h-0"
      >
        {open ? 'Hide summary' : 'Summary'}
      </button>
      {open && (
        <div id={panelId} className="mt-2 rounded-sm border px-3 py-2">
          <SummaryContent
            correlationGroupId={correlationGroupId}
            canWrite={canWrite}
            summary={summary}
          />
        </div>
      )}
    </div>
  )
}

function SummaryContent({
  correlationGroupId,
  canWrite,
  summary,
}: {
  correlationGroupId: string
  canWrite: boolean
  summary: ReturnType<typeof useIncidentSummary>
}) {
  const { data } = summary

  if (summary.isLoading || (!data && !summary.isError)) {
    return <Summarizing />
  }

  if (summary.isError || !data) {
    return (
      <Unavailable
        canWrite={canWrite}
        onRetry={() => void summary.refetch()}
        pending={false}
      />
    )
  }

  const body = data.summary
  if (!body) {
    if (summary.isEnsuring || summary.isRegenerating) return <Summarizing />
    // Missing and not yet tried for these facts: the ensure is about to fire.
    // Once it has settled without a body, offer Retry rather than wait forever.
    if (data.state === 'missing' && !summary.ensureError && !summary.ensureAttempted) {
      return <Summarizing />
    }
    return (
      <Unavailable
        canWrite={canWrite}
        onRetry={summary.retry}
        pending={summary.isEnsuring}
      />
    )
  }

  const updating = data.state === 'stale' && summary.isEnsuring
  return (
    <div className="space-y-2">
      <SummaryBody body={body} correlationGroupId={correlationGroupId} />
      <div className="flex flex-wrap items-center gap-2 text-micro text-fg-tertiary">
        <span>Generated {formatDateTime(body.generated_at)}</span>
        {(updating || summary.isRegenerating) && (
          <span role="status">{summary.isRegenerating ? 'Regenerating…' : 'Updating…'}</span>
        )}
        {!updating && !summary.isRegenerating && summary.updateFailed && (
          <span role="status">Could not update the summary; showing the previous one.</span>
        )}
        {canWrite && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className={SMALL_BUTTON}
            disabled={summary.isRegenerating || summary.isEnsuring}
            onClick={summary.regenerate}
          >
            Regenerate
          </Button>
        )}
      </div>
    </div>
  )
}

function Summarizing() {
  return (
    <p role="status" className="text-body-sm text-fg-tertiary">
      Summarizing…
    </p>
  )
}

function Unavailable({
  canWrite,
  onRetry,
  pending,
}: {
  canWrite: boolean
  onRetry: () => void
  pending: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <p className="text-body-sm text-fg-tertiary">Summary unavailable.</p>
      {canWrite && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={SMALL_BUTTON}
          disabled={pending}
          onClick={onRetry}
        >
          Retry
        </Button>
      )}
    </div>
  )
}

function factAnchor(correlationGroupId: string, factId: number): string {
  return `incident-summary-${correlationGroupId}-fact-${factId}`
}

function SummaryBody({
  body,
  correlationGroupId,
}: {
  body: IncidentSummaryBody
  correlationGroupId: string
}) {
  const factsById = new Map(body.facts.map(fact => [fact.id, fact]))
  return (
    <>
      <p className="text-body-sm">
        {body.sentences.map((sentence, index) => (
          <span
            // Sentences carry no id; their order is the body's own.
            key={index}
            className={cn(!sentence.generated && 'text-fg-tertiary')}
          >
            {index > 0 && ' '}
            {sentence.text}
            {sentence.fact_ids.map(factId => {
              const fact = factsById.get(factId)
              return fact ? (
                <sup key={factId} className="ml-0.5">
                  <FactLink fact={fact} correlationGroupId={correlationGroupId} />
                </sup>
              ) : null
            })}
          </span>
        ))}
      </p>
      {body.facts.length > 0 && (
        <div>
          <h4 className="text-caption font-medium text-fg-tertiary">Sources</h4>
          <ol className="mt-1 space-y-1 text-micro text-fg-tertiary">
            {body.facts.map(fact => (
              <li key={fact.id} id={factAnchor(correlationGroupId, fact.id)}>
                <span className="mr-1 tabular-nums">[{fact.id}]</span>
                {fact.href ? (
                  <Link
                    to={fact.href}
                    className="underline underline-offset-2 hover:text-foreground"
                  >
                    {fact.text}
                  </Link>
                ) : (
                  fact.text
                )}
              </li>
            ))}
          </ol>
        </div>
      )}
    </>
  )
}

function FactLink({
  fact,
  correlationGroupId,
}: {
  fact: IncidentSummaryFact
  correlationGroupId: string
}) {
  const label = `Source ${fact.id}: ${fact.text}`
  const className = 'text-micro underline underline-offset-2 hover:text-foreground'
  if (fact.href) {
    return (
      <Link to={fact.href} aria-label={label} className={className}>
        [{fact.id}]
      </Link>
    )
  }
  // A fact with no page of its own points at its line in the Sources list.
  return (
    <a href={`#${factAnchor(correlationGroupId, fact.id)}`} aria-label={label} className={className}>
      [{fact.id}]
    </a>
  )
}
