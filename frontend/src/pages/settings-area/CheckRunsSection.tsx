import { useEffect, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'

import { checkRunsApi } from '@/api/checkRuns'
import { useAuth } from '@/components/auth-context'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { Field, NativeSelect, SCard, SHeader, SettingsSaveBar, TextInput } from '@/components/settings/kit'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ReadOnlyNotice } from '@/components/states'
import { OffsetPager } from '@/components/ui/offset-pager'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useOffsetPaging } from '@/hooks/useOffsetPaging'
import { codeUrl } from '@/lib/codeLinks'
import { formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { canWriteProject } from '@/lib/permissions'
import { countOf } from '@/lib/plural'
import {
  checkRunDirectoriesKey,
  checkRunSettingsKey,
  checkRunsKey,
  checkRunsRootKey,
  projectQueryOptions,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { shortSha } from '@/lib/checkRuns'
import { CHECK_UPLOAD_DOCS_URL } from '@/lib/docsSite'
import type { CheckRun } from '@/types'

const PAGE_SIZE = 50
const TEMPLATE_INPUT_ID = 'check-runs-code-url-template'
const REPOSITORY_FILTER_ID = 'check-runs-repository'
const DIRECTORY_FILTER_ID = 'check-runs-directory'
const TEMPLATE_EXAMPLE = 'https://github.com/org/app/blob/{commit}/{path}#L{line}'

const linkClass = 'font-medium text-accent underline underline-offset-2'

/** A check directory as the page names it: the repository root reads as such. */
function directoryLabel(checkRoot: string): string {
  return checkRoot === '.' ? 'Repository root' : checkRoot
}

/**
 * Project · Check runs: the runs `tripl check --upload` recorded, newest
 * first, filterable by repository and check directory, and the link template
 * that turns each place "used in code" lists on an event into a link to the
 * file. Everyone in the project reads both; an editor sets the template.
 */
export default function CheckRunsSection({ slug }: { slug: string }) {
  const { user } = useAuth()
  const projectQuery = useQuery(projectQueryOptions(slug))
  const canEdit = projectQuery.isSuccess && canWriteProject(user, projectQuery.data)

  return (
    <div>
      <SHeader
        title="Check runs"
        description="What tripl check found in CI, uploaded with tripl check --upload. The latest run on each repository's default branch is what an event's Used in code shows; every other run is kept as history, for 90 days by default."
      />
      {projectQuery.isSuccess && !canEdit && <ReadOnlyNotice className="mb-4" />}
      <CodeLinkCard slug={slug} canEdit={canEdit} />
      <HistoryCard slug={slug} />
    </div>
  )
}

function CodeLinkCard({ slug, canEdit }: { slug: string; canEdit: boolean }) {
  const qc = useQueryClient()
  const settingsQuery = useQuery({
    queryKey: checkRunSettingsKey(slug),
    queryFn: ({ signal }) => checkRunsApi.getSettings(slug, signal),
    meta: SILENT_ERROR_META,
  })
  const stored = settingsQuery.data?.code_url_template ?? ''
  const [draft, setDraft] = useState<string | null>(null)
  const value = draft ?? stored
  const dirty = canEdit && draft !== null && draft.trim() !== stored
  const [saved, setSaved] = useState(false)

  const saveMut = useMutation({
    // Shown in the save bar: a toast as well would say it twice.
    meta: SILENT_ERROR_META,
    mutationFn: (template: string) =>
      checkRunsApi.updateSettings(slug, { code_url_template: template.trim() || null }),
    onSuccess: (next) => {
      qc.setQueryData(checkRunSettingsKey(slug), next)
      setDraft(null)
      setSaved(true)
      // "Used in code" carries the template, so every event's copy is stale.
      return qc.invalidateQueries({ queryKey: checkRunsRootKey(slug) })
    },
  })

  // An unsaved template arms the settings shell's leave guard.
  const { registerUnsaved } = useUnsavedChanges()
  useEffect(() => {
    registerUnsaved(
      dirty
        ? {
            keptBy: () => false,
            message: 'The link template has not been saved.',
            dirtyPaths: ['project/check-runs'],
          }
        : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, registerUnsaved])

  const example = codeUrl(value.trim(), {
    repositoryUrl: 'https://git.example.com/org/app',
    repository: 'org/app',
    ref: 'main',
    commit: '0123456789abcdef0123456789abcdef01234567',
    path: 'src/checkout/Cart.ts',
    line: 42,
  })

  return (
    <>
      {canEdit && settingsQuery.isSuccess && (
        <SettingsSaveBar
          className="mb-4"
          note={
            saved && !dirty ? (
              <span className="text-success">Saved</span>
            ) : dirty ? (
              <span className="text-warning">Unsaved changes</span>
            ) : (
              'Saves the link template.'
            )
          }
          error={saveMut.isError ? getErrorMessage(saveMut.error) : undefined}
          dirty={dirty}
          pending={saveMut.isPending}
          onDiscard={() => {
            setDraft(null)
            saveMut.reset()
          }}
          onSave={() => {
            if (dirty) saveMut.mutate(value)
          }}
        />
      )}
      <SCard
        title="Links to the code"
        description="How Used in code on an event links each file and line to your repository. Without a template, the places are plain text."
      >
        {settingsQuery.isPending ? (
          <div className="px-4 py-3" role="status" aria-busy="true" aria-label="Loading the link template">
            <Skeleton className="h-8 w-full" />
          </div>
        ) : settingsQuery.isError ? (
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load the link template"
              error={settingsQuery.error}
              onRetry={() => {
                void settingsQuery.refetch()
              }}
            />
          </div>
        ) : (
          <form
            noValidate
            onSubmit={(e) => {
              e.preventDefault()
              if (dirty) saveMut.mutate(value)
            }}
          >
            <Field
              label="Repository URL template"
              htmlFor={TEMPLATE_INPUT_ID}
              stacked
              last
              hint={
                <>
                  An http(s) address with <code>{'{path}'}</code>, and any of{' '}
                  <code>{'{line}'}</code>, <code>{'{commit}'}</code>, <code>{'{ref}'}</code>,{' '}
                  <code>{'{repository}'}</code> (as the CI names it, <code>org/app</code>) and{' '}
                  <code>{'{repository_url}'}</code> (the address the CI sent, at the start only).
                  For GitLab, <code>{'{repository_url}/-/blob/{commit}/{path}#L{line}'}</code>.
                  {example && (
                    <>
                      {' '}
                      Example link: <span className="mono break-all">{example}</span>
                    </>
                  )}
                </>
              }
            >
              <TextInput
                id={TEMPLATE_INPUT_ID}
                value={value}
                onChange={(next) => {
                  setDraft(next)
                  setSaved(false)
                }}
                placeholder={TEMPLATE_EXAMPLE}
                mono
                readOnly={!canEdit}
              />
            </Field>
          </form>
        )}
      </SCard>
    </>
  )
}

function HistoryCard({ slug }: { slug: string }) {
  const [repository, setRepository] = useState('')
  const [checkRoot, setCheckRoot] = useState('')
  const [offset, setOffset] = useState(0)

  const directoriesQuery = useQuery({
    queryKey: checkRunDirectoriesKey(slug),
    queryFn: ({ signal }) => checkRunsApi.directories(slug, signal),
    meta: SILENT_ERROR_META,
  })
  const params = { repository: repository || undefined, checkRoot: checkRoot || undefined, offset }
  const listQuery = useQuery({
    queryKey: checkRunsKey(slug, params),
    queryFn: ({ signal }) => checkRunsApi.list(slug, { ...params, limit: PAGE_SIZE }, signal),
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })

  const directories = directoriesQuery.data?.items ?? []
  const repositories = [...new Set(directories.map((item) => item.repository))]
  const checkRoots = [
    ...new Set(
      directories
        .filter((item) => !repository || item.repository === repository)
        .map((item) => item.check_root),
    ),
  ]
  const items = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const paging = useOffsetPaging({ query: listQuery, offset, shown: items.length, total })
  const filtered = !!(repository || checkRoot)

  return (
    <SCard
      title="History"
      description={
        listQuery.isSuccess
          ? `${countOf(total, 'run', 'runs')}${filtered ? ' match the filters' : ''}, newest first.`
          : 'Every uploaded run, newest first.'
      }
    >
      {directories.length > 0 && (
        <div className="flex flex-wrap items-end gap-3 border-b px-4 py-3 border-border-subtle">
          <div className="min-w-[200px]">
            <label htmlFor={REPOSITORY_FILTER_ID} className="mb-1 block text-caption text-fg-tertiary">
              Repository
            </label>
            <NativeSelect
              id={REPOSITORY_FILTER_ID}
              size="sm"
              value={repository}
              onChange={(next) => {
                setRepository(next)
                // A directory of another repository would match nothing.
                if (next && checkRoot && !directories.some((d) => d.repository === next && d.check_root === checkRoot)) {
                  setCheckRoot('')
                }
                setOffset(0)
              }}
              options={[{ value: '', label: 'All repositories' }, ...repositories]}
            />
          </div>
          <div className="min-w-[200px]">
            <label htmlFor={DIRECTORY_FILTER_ID} className="mb-1 block text-caption text-fg-tertiary">
              Check directory
            </label>
            <NativeSelect
              id={DIRECTORY_FILTER_ID}
              size="sm"
              value={checkRoot}
              onChange={(next) => {
                setCheckRoot(next)
                setOffset(0)
              }}
              options={[
                { value: '', label: 'All check directories' },
                ...checkRoots.map((root) => ({ value: root, label: directoryLabel(root) })),
              ]}
            />
          </div>
        </div>
      )}

      {listQuery.isPending ? (
        <div className="space-y-2 px-4 py-3" role="status" aria-busy="true" aria-label="Loading check runs">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-6 w-full" />
          ))}
        </div>
      ) : listQuery.isError ? (
        <div className="p-4">
          <ErrorState
            compact
            title="Couldn't load the check runs"
            error={listQuery.error}
            onRetry={() => {
              void listQuery.refetch()
            }}
          />
        </div>
      ) : items.length === 0 ? (
        filtered ? (
          <EmptyState size="sm" headingLevel={3} title="No runs match" description="No uploaded run is kept for this repository and check directory." />
        ) : (
          <EmptyState
            size="sm"
            headingLevel={3}
            title="No check runs yet"
            description="Run tripl check --upload in CI with a check upload key, and each run shows here."
            action={
              <a href={CHECK_UPLOAD_DOCS_URL} target="_blank" rel="noreferrer" className={linkClass}>
                How to upload check runs
                <ExternalLink className="ml-0.5 inline size-3" aria-hidden="true" />
                <span className="sr-only"> (opens in a new tab)</span>
              </a>
            }
          />
        )
      ) : (
        <>
          <Table>
            <TableCaption className="sr-only">Uploaded check runs, newest first</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Uploaded</TableHead>
                <TableHead scope="col">Repository</TableHead>
                <TableHead scope="col">Ref</TableHead>
                <TableHead scope="col">Commit</TableHead>
                <TableHead scope="col" className="text-right">Calls</TableHead>
                <TableHead scope="col" className="text-right">Calls with findings</TableHead>
                <TableHead scope="col">Kept as</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </TableBody>
          </Table>
          {(paging.hasPrev || paging.hasNext) && (
            <OffsetPager
              className="border-t px-4 py-2.5 border-border-subtle"
              paging={paging}
              caption={`Showing ${paging.rangeStart}–${paging.rangeEnd} of ${total} runs.`}
              prevLabel="Newer"
              nextLabel="Older"
              onPrev={() => setOffset(Math.max(0, paging.settledOffset - PAGE_SIZE))}
              onNext={() => setOffset(paging.settledOffset + PAGE_SIZE)}
              label="Check run pages"
            />
          )}
        </>
      )}
    </SCard>
  )
}

function RunRow({ run }: { run: CheckRun }) {
  const { calls } = run
  // Calls per verdict: the summary a run keeps, whatever its kind.
  const findings = [
    calls.error > 0 ? `${calls.error} error` : null,
    calls.warning > 0 ? `${calls.warning} warning` : null,
    calls.dynamic > 0 ? `${calls.dynamic} not read` : null,
  ].filter((part): part is string => part !== null)
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap text-body-sm">
        <time dateTime={run.created_at}>{formatTimestamp(run.created_at)}</time>
      </TableCell>
      <TableCell className="text-body-sm">
        <div className="break-all">{run.repository}</div>
        {run.check_root !== '.' && (
          <div className="break-all text-caption text-fg-tertiary">{run.check_root}</div>
        )}
      </TableCell>
      <TableCell className="text-body-sm">
        <div className="mono break-all">{run.ref}</div>
        <div className="mt-0.5">
          {run.merge_request ? (
            <Chip tone="info" size="sm">Merge request</Chip>
          ) : run.is_default_branch ? (
            <Chip tone="accent" size="sm">Default branch</Chip>
          ) : (
            <Chip tone="neutral" size="sm">Branch</Chip>
          )}
        </div>
      </TableCell>
      <TableCell className="whitespace-nowrap text-body-sm">
        <span className="mono" title={run.commit_sha}>
          {shortSha(run.commit_sha)}
        </span>
        {run.pipeline_url && (
          <div>
            <a href={run.pipeline_url} target="_blank" rel="noreferrer" className={linkClass}>
              Pipeline
              <span className="sr-only"> of {shortSha(run.commit_sha)} (opens in a new tab)</span>
            </a>
          </div>
        )}
      </TableCell>
      <TableCell className="text-right text-body-sm tabular-nums">{calls.total}</TableCell>
      <TableCell className="text-right text-body-sm">
        {findings.length === 0 ? <span className="text-fg-tertiary">None</span> : findings.join(', ')}
      </TableCell>
      <TableCell className="text-body-sm">
        {run.is_current ? (
          <Chip tone="success" size="sm">Current</Chip>
        ) : (
          <span className="text-fg-tertiary">History only</span>
        )}
      </TableCell>
    </TableRow>
  )
}
