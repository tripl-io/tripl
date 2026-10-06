import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, GitMerge, Info } from 'lucide-react'

import { ApiError } from '@/api/client'
import { planBranchesApi } from '@/api/planBranches'
import { Chip } from '@/components/primitives/chip'
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { planBranchMergePreviewKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type {
  MergePreviewTarget,
  MergedEventOutcome,
  MergedEventPreview,
  MergedProperty,
  MergedState,
  MergedValue,
} from '@/types'
import { DiffValue } from '../DiffValue'
import {
  HIDE_UNCHANGED_THRESHOLD,
  attributeLabel,
  stateMeta,
  visibleRows,
} from './mergedEventModel'

const OUTCOME_LABEL: Record<MergedEventOutcome, { label: string; tone: MergedState }> = {
  added: { label: 'New on main', tone: 'added' },
  changed: { label: 'Changes on main', tone: 'changed' },
  unchanged: { label: 'No change on main', tone: 'unchanged' },
  removed: { label: 'Gone from main', tone: 'removed' },
  skipped: { label: 'Not created', tone: 'removed' },
}

interface MergedEventSheetProps {
  slug: string
  branchId: string
  /** Null while closed. */
  target: MergePreviewTarget | null
  onClose: () => void
  /** Called once with the event's own id when the sheet was opened by type and
   * name, so the address can name it by id from then on. */
  onResolved?: (refId: string) => void
  /** Closes the sheet and brings the conflicts list into view. */
  onShowConflicts?: () => void
  /** Present only where "Update from main" can run. */
  onUpdateFromMain?: () => void
}

/** One event as main will hold it after this branch merges — see mergedEventModel. */
export function MergedEventSheet({
  slug,
  branchId,
  target,
  onClose,
  onResolved,
  onShowConflicts,
  onUpdateFromMain,
}: MergedEventSheetProps) {
  const query = useQuery({
    queryKey: planBranchMergePreviewKey(slug, branchId, target),
    queryFn: () => planBranchesApi.mergePreview(slug, branchId, target as MergePreviewTarget),
    enabled: target !== null,
    staleTime: 30_000,
    // A 409 is the answer (the merge's own refusal), not a hiccup to retry.
    retry: false,
  })
  const data = target ? query.data : undefined
  // Once per key target and opening: the target object is rebuilt from the
  // address on every render, so it is compared by what it names, and the
  // mark is cleared when the address stops naming it (the sheet stays
  // mounted while closed), so opening the same link again resolves again.
  const resolvedFor = useRef<string | null>(null)
  const keyTarget = target && !('eventId' in target) ? `${target.eventType}\u0000${target.eventName}` : null
  useEffect(() => {
    if (!keyTarget) {
      resolvedFor.current = null
      return
    }
    if (!data || !onResolved) return
    if (resolvedFor.current === keyTarget) return
    resolvedFor.current = keyTarget
    onResolved(data.ref_id)
  }, [data, keyTarget, onResolved])

  const titleName = data?.name ?? (target && 'eventName' in target ? target.eventName : null)

  return (
    <Sheet open={target !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent side="right" className="sm:w-[min(720px,100vw)]">
        <SheetHeader>
          <SheetTitle>
            {titleName ? (
              <>
                Event after merge: <span className="mono">{titleName}</span>
              </>
            ) : (
              'Event after merge'
            )}
          </SheetTitle>
          <SheetDescription>
            How main will hold this event once the branch merges. “Before” is main as it is now.
          </SheetDescription>
        </SheetHeader>
        <SheetBody>
          {query.isPending && target ? (
            <div aria-hidden="true" className="space-y-2.5 py-2">
              <Skeleton className="h-4 w-3/5" />
              <Skeleton className="h-4 w-4/5" />
              <Skeleton className="h-4 w-2/5" />
            </div>
          ) : query.isError ? (
            <PreviewError error={query.error} retry={() => void query.refetch()} />
          ) : data ? (
            <PreviewBody
              data={data}
              onShowConflicts={onShowConflicts}
              onUpdateFromMain={onUpdateFromMain}
            />
          ) : null}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}

function PreviewError({ error, retry }: { error: unknown; retry: () => void }) {
  // The merge's own refusals (an old base, a landed branch, a namesake) carry
  // their reason; show it instead of a projection nobody can merge.
  const detail = error instanceof ApiError && error.status === 409 ? error.detail : undefined
  const message =
    detail && typeof detail === 'object' && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : null
  if (message) {
    return (
      <p role="note" className="flex items-start gap-1.5 py-2 text-body-sm text-warning">
        <AlertTriangle className="mt-[3px] size-3.5 shrink-0" aria-hidden="true" />
        <span>{message}</span>
      </p>
    )
  }
  return (
    <p role="alert" className="py-2 text-caption text-danger">
      Could not load this event as merged: {getErrorMessage(error)}{' '}
      <button type="button" onClick={retry} className="font-medium underline">
        Retry
      </button>
    </p>
  )
}

function PreviewBody({
  data,
  onShowConflicts,
  onUpdateFromMain,
}: {
  data: MergedEventPreview
  onShowConflicts?: () => void
  onUpdateFromMain?: () => void
}) {
  const attributes = data.attributes ?? []
  const fieldValues = data.field_values ?? []
  const metaValues = data.meta_values ?? []
  const tags = data.tags ?? []
  const properties = data.properties ?? []
  const notes = data.notes ?? []
  const unchangedCount = [...attributes, ...fieldValues, ...metaValues, ...tags, ...properties].filter(
    (row) => row.state === 'unchanged',
  ).length
  const [hideChoice, setHideChoice] = useState<boolean | null>(null)
  const hideUnchanged = hideChoice ?? unchangedCount > HIDE_UNCHANGED_THRESHOLD
  const outcome = OUTCOME_LABEL[data.outcome]
  const others = data.other_blocking_count ?? 0

  return (
    <div className="flex flex-col gap-4 pb-4">
      <div className="flex flex-wrap items-center gap-2 text-caption text-fg-tertiary">
        <span className="mono">{data.event_type_name}</span>
        <Chip tone={stateMeta(outcome.tone).tone ?? 'neutral'} size="xs">
          {outcome.label}
        </Chip>
        {data.previous_name ? (
          <span>
            renamed from <span className="mono text-fg-secondary">{data.previous_name}</span>
          </span>
        ) : null}
      </div>

      {data.blocked ? (
        <Banner tone="danger">
          This event conflicts with main. The branch cannot merge until you update from main.{' '}
          <a
            href="#branch-conflicts"
            onClick={(event) => {
              if (!onShowConflicts) return
              event.preventDefault()
              onShowConflicts()
            }}
            className="font-medium underline"
          >
            See the conflicts
          </a>
          {onUpdateFromMain ? (
            <>
              {' · '}
              <button type="button" onClick={onUpdateFromMain} className="font-medium underline">
                Update from main
              </button>
            </>
          ) : null}
        </Banner>
      ) : data.branch_merge_blocked ? (
        <Banner tone="warning">
          This event is clean, but{' '}
          {others === 1 ? '1 other conflict blocks' : `${others} other conflicts block`} the whole
          merge: a merge lands all of the branch or none of it.
        </Banner>
      ) : data.behind_base ? (
        <Banner tone="info">Includes main’s edits made after this branch was cut.</Banner>
      ) : null}

      {notes.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {notes.map((note) => (
            <li key={note} role="note" className="flex items-start gap-1.5 text-caption text-fg-secondary">
              <Info className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
              <span>{note}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {unchangedCount > 0 ? (
        <label className="flex w-fit items-center gap-2 text-caption text-fg-secondary">
          <input
            type="checkbox"
            checked={hideUnchanged}
            onChange={(event) => setHideChoice(event.target.checked)}
          />
          Hide unchanged ({unchangedCount})
        </label>
      ) : null}

      <ValueSection
        title="Attributes"
        rows={visibleRows(attributes, hideUnchanged)}
        labelOf={attributeLabel}
      />
      <ValueSection title="Field values" rows={visibleRows(fieldValues, hideUnchanged)} mono />
      <ValueSection title="Meta values" rows={visibleRows(metaValues, hideUnchanged)} mono />
      <TagSection rows={visibleRows(tags, hideUnchanged)} />
      <PropertySection rows={visibleRows(properties, hideUnchanged)} />
    </div>
  )
}

function Banner({ tone, children }: { tone: 'danger' | 'warning' | 'info'; children: ReactNode }) {
  const Icon = tone === 'info' ? GitMerge : AlertTriangle
  return (
    <p
      role={tone === 'info' ? 'note' : 'alert'}
      className="flex items-start gap-1.5 rounded-sm border px-3 py-2 text-caption"
      style={{
        borderColor: `color-mix(in oklab, var(--${tone}) 40%, transparent)`,
        background: `color-mix(in oklab, var(--${tone}) 8%, transparent)`,
        color: `var(--${tone === 'info' ? 'fg' : tone})`,
      }}
    >
      <Icon className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </p>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h3 className="mb-1.5 micro-label text-fg-tertiary">{title}</h3>
      {children}
    </section>
  )
}

export function StateChip({ state }: { state: MergedState }) {
  const meta = stateMeta(state)
  if (!meta.tone) return <span className="sr-only">{meta.label}</span>
  return (
    <Chip tone={meta.tone} size="xs">
      {meta.label}
    </Chip>
  )
}

function ValueSection({
  title,
  rows,
  labelOf,
  mono,
}: {
  title: string
  rows: MergedValue[]
  labelOf?: (key: string) => string
  mono?: boolean
}) {
  if (rows.length === 0) return null
  return (
    <Section title={title}>
      <dl className="grid grid-cols-1 gap-x-3 gap-y-2 sm:grid-cols-[minmax(0,160px)_1fr]">
        {rows.map((row) => (
          <div key={row.key} className="contents">
            <dt className={`truncate text-caption text-fg-tertiary ${mono ? 'mono' : ''}`} title={row.key}>
              {labelOf ? labelOf(row.key) : row.key}
            </dt>
            <dd className="min-w-0 break-words">
              <MergedValueCell row={row} />
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  )
}

function MergedValueCell({ row }: { row: MergedValue }) {
  const meta = stateMeta(row.state)
  return (
    <div className="flex flex-col gap-0.5">
      {row.state === 'conflict' ? (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <div>
            <div className="text-caption text-fg-tertiary">main now</div>
            <DiffValue value={row.previous} />
          </div>
          <div>
            <div className="text-caption text-fg-tertiary">this branch</div>
            <DiffValue value={row.branch_value} tone="danger" />
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-baseline gap-2">
          {row.state === 'removed' ? (
            <del className="text-fg-tertiary" title="main now">
              <DiffValue value={row.previous} />
            </del>
          ) : (
            <DiffValue value={row.value} tone={meta.tone && meta.tone !== 'neutral' ? meta.tone : undefined} />
          )}
          {row.state === 'changed' ? (
            <del className="text-fg-tertiary" title={`main now: ${plain(row.previous)}`}>
              <DiffValue value={row.previous} />
            </del>
          ) : null}
          <StateChip state={row.state} />
        </div>
      )}
      {row.main_moved ? (
        <span className="text-caption text-info">changed on main since this branch</span>
      ) : null}
      {row.note ? <span className="text-caption text-fg-secondary">{row.note}</span> : null}
      {row.branch_value !== undefined && row.branch_value !== null && row.state !== 'conflict' ? (
        <span className="text-caption text-fg-tertiary">
          this branch has <span className="mono">{plain(row.branch_value)}</span>
        </span>
      ) : null}
    </div>
  )
}

function plain(value: unknown): string {
  if (value === null || value === undefined || value === '') return '∅'
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function TagSection({ rows }: { rows: MergedValue[] }) {
  if (rows.length === 0) return null
  return (
    <Section title="Tags">
      <ul className="flex flex-wrap gap-1.5">
        {rows.map((row) => {
          const meta = stateMeta(row.state)
          return (
            <li key={row.key}>
              <Chip tone={meta.tone ?? 'neutral'} size="xs">
                <span className={meta.strike ? 'line-through' : undefined}>{row.key}</span>
                <span className="sr-only">, {meta.label}</span>
              </Chip>
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

function PropertySection({ rows }: { rows: MergedProperty[] }) {
  if (rows.length === 0) return null
  return (
    <Section title="Properties">
      <div className="overflow-x-auto">
        <table className="w-full text-caption">
          <thead>
            <tr className="text-left text-fg-tertiary">
              <th className="py-1 pr-3 font-normal">Name</th>
              <th className="py-1 pr-3 font-normal">Type</th>
              <th className="py-1 pr-3 font-normal">Required</th>
              <th className="py-1 font-normal">Documented values</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((prop) => (
              <tr key={prop.name} className="border-t border-border-subtle align-top">
                <td className="py-1.5 pr-3">
                  <span className={`mono ${stateMeta(prop.state).strike ? 'line-through' : ''}`}>
                    {prop.name}
                  </span>{' '}
                  <StateChip state={prop.state} />
                  {prop.main_moved ? (
                    <div className="text-info">changed on main since this branch</div>
                  ) : null}
                  {prop.variable_state === 'conflict' && prop.state !== 'conflict' ? (
                    // The variable clashes on an attribute the event does not
                    // hold (its description, say); the notes name it.
                    <div className="text-danger">variable conflicts with main</div>
                  ) : null}
                </td>
                <td className="py-1.5 pr-3 mono">{prop.variable_type}</td>
                <td className="py-1.5 pr-3">
                  {prop.required ? 'Required' : 'Optional'}
                  {prop.previous_required !== null &&
                  prop.previous_required !== undefined &&
                  prop.previous_required !== prop.required ? (
                    <del className="ml-1 text-fg-tertiary" title="main now">
                      {prop.previous_required ? 'Required' : 'Optional'}
                    </del>
                  ) : null}
                </td>
                <td className="py-1.5">
                  <ul className="flex flex-wrap gap-1" aria-label={`${prop.name} values`}>
                    {(prop.values ?? []).map((item) => {
                      const meta = stateMeta(item.state)
                      return (
                        <li key={`${item.value}-${item.state}`}>
                          <Chip tone={meta.tone ?? 'neutral'} size="xs">
                            <span className={`mono ${meta.strike ? 'line-through' : ''}`}>
                              {item.value}
                            </span>
                            {meta.tone ? (
                              <span className="ml-1">{meta.label}</span>
                            ) : (
                              <span className="sr-only">, {meta.label}</span>
                            )}
                          </Chip>
                        </li>
                      )
                    })}
                  </ul>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}
