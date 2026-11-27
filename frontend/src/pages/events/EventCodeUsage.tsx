import { useId } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'

import { checkRunsApi } from '@/api/checkRuns'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { Skeleton } from '@/components/ui/skeleton'
import { checkRunsSettingsPath, shortSha } from '@/lib/checkRuns'
import { codeUrl } from '@/lib/codeLinks'
import { formatRelativeTime, formatTimestamp } from '@/lib/datetime'
import { CHECK_UPLOAD_DOCS_URL } from '@/lib/docsSite'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventCodeUsageKey } from '@/lib/queryKeys'
import type { CheckRunCallLocation, EventCodeUsageRepository } from '@/types'
import { SurfCard } from './eventFormLayout'

const STATUS_LABEL: Record<CheckRunCallLocation['status'], string | null> = {
  ok: null,
  warning: 'Warning',
  error: 'Error',
  dynamic: 'Name not read',
}

const STATUS_TONE = { warning: 'warning', error: 'danger', dynamic: 'neutral' } as const

const linkClass = 'font-medium text-accent underline underline-offset-2'

/**
 * "Used in code" on an event: per repository and check directory, the places
 * in its current default-branch check run that send the event, as file:line.
 * Each place links to the file through the project's link template, and is
 * plain text without one.
 *
 * `eventId` is the event on main: a check run resolves its calls against the
 * main plan, so a branch copy asks with its main twin's id.
 */
export function EventCodeUsage({ slug, eventId }: { slug: string; eventId: string }) {
  const query = useQuery({
    queryKey: eventCodeUsageKey(slug, eventId),
    queryFn: ({ signal }) => checkRunsApi.codeUsage(slug, eventId, signal),
    // Rendered in the card, with a retry: a toast as well would say it twice.
    meta: SILENT_ERROR_META,
  })

  return (
    <SurfCard
      title="Used in code"
      subtitle="Where the latest check run on each repository's default branch finds this event."
    >
      {query.isPending ? (
        <div className="space-y-2 px-4 py-3" role="status" aria-busy="true" aria-label="Loading where the code sends this event">
          <Skeleton className="h-3 w-48" />
          <Skeleton className="h-3 w-64" />
        </div>
      ) : query.isError ? (
        <div className="p-4">
          <ErrorState
            compact
            title="Couldn't load where the code sends this event"
            error={query.error}
            onRetry={() => {
              void query.refetch()
            }}
          />
        </div>
      ) : query.data.repositories.length === 0 ? (
        <p className="m-0 px-4 py-3 text-body-sm leading-[1.5] text-fg-tertiary">
          No check run from a default branch has been uploaded to this project yet, so tripl
          does not know where the code sends this event.{' '}
          <a href={CHECK_UPLOAD_DOCS_URL} target="_blank" rel="noreferrer" className={linkClass}>
            Upload check runs from CI
            <ExternalLink className="ml-0.5 inline size-3" aria-hidden="true" />
            <span className="sr-only"> (opens in a new tab)</span>
          </a>
        </p>
      ) : (
        <>
          {query.data.repositories.map((repository) => (
            <RepositoryUsage
              key={`${repository.repository}\u0000${repository.check_root}`}
              repository={repository}
              template={query.data.code_url_template ?? null}
            />
          ))}
          {!query.data.code_url_template &&
            query.data.repositories.some((repository) => repository.calls.length > 0) && (
              <p className="m-0 border-t px-4 py-2.5 text-caption text-fg-tertiary border-border-subtle">
                To open each place in the repository, set a link template in{' '}
                <Link to={checkRunsSettingsPath(slug)} className={linkClass}>
                  Settings › Check runs
                </Link>
                .
              </p>
            )}
        </>
      )}
    </SurfCard>
  )
}

function RepositoryUsage({
  repository,
  template,
}: {
  repository: EventCodeUsageRepository
  template: string | null
}) {
  const headingId = useId()
  const { run } = repository
  const directory = repository.check_root === '.' ? null : repository.check_root
  return (
    <section
      aria-labelledby={headingId}
      className="border-b px-4 py-3 last:border-0 border-border-subtle"
    >
      <h3 id={headingId} className="m-0 text-body-sm font-medium break-all">
        {repository.repository}
        {directory && <span className="font-normal text-fg-tertiary"> · {directory}</span>}
      </h3>
      <p className="m-0 mt-0.5 text-caption text-fg-tertiary">
        <span className="mono">{run.ref}</span> at{' '}
        <span className="mono" title={run.commit_sha}>
          {shortSha(run.commit_sha)}
        </span>
        , checked{' '}
        <time dateTime={run.created_at} title={formatTimestamp(run.created_at, { zone: true })}>
          {formatRelativeTime(run.created_at)}
        </time>
        {run.pipeline_url && (
          <>
            {' · '}
            <a href={run.pipeline_url} target="_blank" rel="noreferrer" className={linkClass}>
              Pipeline
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </>
        )}
      </p>
      {repository.calls.length === 0 ? (
        <p className="m-0 mt-1.5 text-body-sm text-fg-tertiary">No call sends this event here.</p>
      ) : (
        <ul className="m-0 mt-1.5 list-none space-y-1 p-0">
          {repository.calls.map((call, index) => {
            const place = `${call.path}:${call.line}`
            const href = codeUrl(template, {
              repositoryUrl: repository.repository_url,
              repository: repository.repository,
              ref: run.ref,
              commit: run.commit_sha,
              path: call.path,
              line: call.line,
            })
            const status = STATUS_LABEL[call.status]
            return (
              <li
                // Two calls may share a line (and a column, or have none),
                // and the list is the run's, fixed in order: the index keeps
                // each key unique.
                key={`${index}:${call.path}:${call.line}`}
                className="flex flex-wrap items-center gap-2 text-body-sm"
              >
                {href ? (
                  <a
                    href={href}
                    target="_blank"
                    rel="noreferrer"
                    className="mono break-all text-accent underline underline-offset-2"
                  >
                    {place}
                    <span className="sr-only"> (opens in a new tab)</span>
                  </a>
                ) : (
                  <code className="mono break-all">{place}</code>
                )}
                {status && call.status !== 'ok' && (
                  <Chip tone={STATUS_TONE[call.status]} size="sm">
                    {status}
                  </Chip>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
