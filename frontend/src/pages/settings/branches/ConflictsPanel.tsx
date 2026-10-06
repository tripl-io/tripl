import { useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'

import { planBranchesApi } from '@/api/planBranches'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { countOf } from '@/lib/plural'
import { getErrorMessage } from '@/lib/utils'
import type {
  PlanBranchConflictEntity,
  PlanBranchConflictField,
  PlanBranchConflicts,
  PlanBranchSummary,
  PlanDiffEntityType,
  ResolutionChoice,
} from '@/types'
import { DiffValue } from '../DiffValue'
import { planBranchConflictsKey } from '@/lib/queryKeys'
import { entityTypeTitle } from './branchDiffModel'
import {
  type BulkScope,
  type BulkStrategy,
  bulkChoices,
  PRESENCE_FIELD,
  presenceLeft,
  withChoice,
  withChoices,
} from './conflictModel'

/**
 * The backend's `ours` is main as it is now and `theirs` is this branch
 * (`plan_branch_conflicts.py`). Product users are not git users, so neither
 * word reaches the screen. A stored choice names the resulting value,
 * so the same two words serve the merge and "Update from main".
 */
const CHOICE_LABEL: Record<ResolutionChoice, string> = {
  theirs: 'Keep this branch',
  ours: 'Take main',
}
const CHOSEN_TEXT: Record<ResolutionChoice, string> = {
  theirs: "Resolved: this branch's value",
  ours: "Resolved: main's value",
}

/** The "for all" actions, in the order shown; `filled` leads on an entity
 * both sides added (`ENTITY_BULK_ORDER_ADDED`). */
const BULK_LABEL: Record<BulkStrategy, string> = {
  theirs: 'Keep this branch for all',
  ours: 'Take main for all',
  filled: 'Keep whichever is filled in',
}
const BULK_ORDER: readonly BulkStrategy[] = ['theirs', 'ours', 'filled']
const ENTITY_BULK_ORDER_ADDED: readonly BulkStrategy[] = ['filled', 'theirs', 'ours']

interface ConflictListProps {
  entities: PlanBranchConflictEntity[]
  /** The choice to show for a field: a local pick, or the stored one. */
  choiceOf: (entity: PlanBranchConflictEntity, field: PlanBranchConflictField) => ResolutionChoice | null
  /** Omitted for a viewer, who sees the values but picks no side. */
  onResolve?: (
    entity: PlanBranchConflictEntity,
    field: PlanBranchConflictField,
    choice: ResolutionChoice,
  ) => void
  pending?: boolean
  /** A field whose own choice is still being saved (the Conflicts panel). */
  pendingOf?: (entity: PlanBranchConflictEntity, field: PlanBranchConflictField) => boolean
  /**
   * A "for all" action, for one entity (`scope`) or the whole list. Value rows
   * only: deletions stay one choice per row. Omitted for a viewer.
   */
  onBulk?: (strategy: BulkStrategy, scope?: BulkScope) => void
  /** The bulk actions wait: a save they could race is still in flight. */
  bulkPending?: boolean
}

/**
 * Every overlap, grouped by entity type and parent ("Fields in checkout"),
 * each field with its three values and the two choices. Shared by the
 * Conflicts panel and the "Update from main" dialog, so a choice reads the
 * same in both.
 */
export function ConflictList({
  entities,
  choiceOf,
  onResolve,
  pending = false,
  pendingOf,
  onBulk,
  bulkPending = false,
}: ConflictListProps) {
  const groups = new Map<string, { title: string; entities: PlanBranchConflictEntity[] }>()
  for (const entity of entities) {
    const parent = entity.parent ?? null
    const key = `${entity.entity_type}\u0000${parent ?? ''}`
    const group = groups.get(key)
    if (group) {
      group.entities.push(entity)
    } else {
      groups.set(key, {
        title: parent
          ? `${entityTypeTitle(entity.entity_type)} in ${parent}`
          : entityTypeTitle(entity.entity_type),
        entities: [entity],
      })
    }
  }
  const deletionsLeft = onBulk ? presenceLeft(entities, choiceOf) : 0
  const valueRows = (list: readonly PlanBranchConflictEntity[]) =>
    list.some((entity) => entity.fields.some((field) => field.field !== PRESENCE_FIELD))
  return (
    <div className="space-y-3">
      {onBulk && entities.length > 1 && valueRows(entities) ? (
        <BulkActions
          label="Every conflict"
          order={BULK_ORDER}
          disabled={pending || bulkPending}
          onBulk={(strategy) => onBulk(strategy)}
        />
      ) : null}
      {deletionsLeft > 0 ? (
        <p className="text-caption text-fg-tertiary" data-testid="deletions-left">
          {countOf(deletionsLeft, 'deletion', 'deletions')} still{' '}
          {deletionsLeft === 1 ? 'needs' : 'need'} a choice — a deletion is always picked on its own row.
        </p>
      ) : null}
      {[...groups.entries()].map(([groupKey, group]) => (
        <section key={groupKey} className="space-y-2">
          <h3 className="text-caption font-medium text-fg-tertiary">{group.title}</h3>
          {group.entities.map((entity) => (
            <div
              // Two entity types may share a name; the type is part of the identity.
              key={`${entity.entity_type}:${entity.name}`}
              className="rounded-card border p-3 border-border-subtle"
            >
              {/* "Event type checkout", not the wire's `event_type: checkout`. */}
              <div className="mb-1 text-body-sm text-fg-tertiary">
                {entityTypeTitle(entity.entity_type)}{' '}
                <span className="mono font-medium text-fg">{entity.label || entity.name}</span>
              </div>
              {entity.added_on_both ? (
                <p className="mb-1 text-caption text-fg-tertiary">
                  Added on both sides — main’s copy may have come from a scan.
                </p>
              ) : null}
              {onBulk && valueRows([entity]) ? (
                <BulkActions
                  label={`All fields of ${entity.label || entity.name}`}
                  order={entity.added_on_both ? ENTITY_BULK_ORDER_ADDED : BULK_ORDER}
                  primary={entity.added_on_both ? 'filled' : undefined}
                  disabled={pending || bulkPending}
                  onBulk={(strategy) =>
                    onBulk(strategy, { entity_type: entity.entity_type, name: entity.name })
                  }
                />
              ) : null}
              <div className="space-y-2">
                {entity.fields.map((field) => (
                  <ConflictFieldRow
                    key={field.field}
                    entity={entity}
                    field={field}
                    choice={choiceOf(entity, field)}
                    pending={pending || (pendingOf?.(entity, field) ?? false)}
                    onResolve={onResolve ? (choice) => onResolve(entity, field, choice) : undefined}
                  />
                ))}
              </div>
            </div>
          ))}
        </section>
      ))}
    </div>
  )
}

/** One row of "for all" actions: a labelled group, so the list's row and each
 * entity's row read apart for a screen reader (and a test). */
function BulkActions({
  label,
  order,
  primary,
  disabled,
  onBulk,
}: {
  label: string
  order: readonly BulkStrategy[]
  /** Filled in as the suggested action; the others stay outlined. */
  primary?: BulkStrategy
  disabled: boolean
  onBulk: (strategy: BulkStrategy) => void
}) {
  return (
    <div role="group" aria-label={label} className="mb-2 flex flex-wrap items-center gap-1.5">
      {order.map((strategy) => (
        <Button
          key={strategy}
          type="button"
          size="sm"
          variant={strategy === primary ? 'default' : 'outline'}
          disabled={disabled}
          onClick={() => onBulk(strategy)}
        >
          {BULK_LABEL[strategy]}
        </Button>
      ))}
    </div>
  )
}

interface ResolveVars {
  entity_type: string
  entity_name: string
  field: string
  choice: ResolutionChoice
}

export function ConflictsPanel({ slug, branch }: { slug: string; branch: PlanBranchSummary }) {
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  // Two plan snapshots per call, and a landed branch has nothing left to
  // resolve — so a merged or closed one never asks.
  const open = branch.status !== 'merged' && branch.status !== 'closed'
  const conflictsKey = planBranchConflictsKey(slug, branch.id)
  const { data: conflicts } = useQuery({
    queryKey: conflictsKey,
    queryFn: () => planBranchesApi.getConflicts(slug, branch.id),
    enabled: open,
  })

  const resolutionMutationKey = ['plan-branch-resolution', slug, branch.id]
  // Rows save side by side, but one field waits for its own save: two in
  // flight for the same field race to insert the same row, and the older
  // one's rollback could undo the newer pick.
  // A single pick's variables are one field, a batch's a list of them: both
  // share the key prefix, so both hold their fields here and in onSettled.
  const savingFields = useMutationState({
    filters: { mutationKey: resolutionMutationKey, status: 'pending' },
    select: (mutation) => {
      const vars = mutation.state.variables as ResolveVars | ResolveVars[] | undefined
      return Array.isArray(vars) ? vars : vars ? [vars] : []
    },
  }).flat()
  const resolutionMut = useMutation({
    mutationKey: resolutionMutationKey,
    // Rendered inline below, beside the choice that failed.
    meta: SILENT_ERROR_META,
    mutationFn: ({ entity_type, entity_name, field, choice }: ResolveVars) =>
      planBranchesApi.saveResolution(slug, branch.id, {
        entity_type,
        entity_name,
        field_name: field,
        choice,
      }),
    // The choice shows the moment it is clicked. Re-reading the conflicts
    // builds two plan snapshots, and waiting on that left every button greyed
    // out with nothing to say which one was pressed.
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: conflictsKey })
      const current = qc.getQueryData<PlanBranchConflicts>(conflictsKey)
      const previous =
        current?.entities
          .find((e) => e.entity_type === vars.entity_type && e.name === vars.entity_name)
          ?.fields.find((f) => f.field === vars.field)?.choice ?? null
      if (current) qc.setQueryData(conflictsKey, withChoice(current, vars, vars.choice))
      return { previous }
    },
    // Undo only this field: restoring a whole snapshot would also undo a
    // choice made on another row while this one was in flight.
    onError: (_error, vars, context) => {
      const current = qc.getQueryData<PlanBranchConflicts>(conflictsKey)
      if (current) {
        qc.setQueryData(conflictsKey, withChoice(current, vars, context?.previous ?? null))
      }
    },
    // One re-read once the last click settles, so an earlier re-read cannot
    // land without a choice still in flight and flick it back.
    onSettled: () => {
      if (qc.isMutating({ mutationKey: resolutionMutationKey }) === 1) {
        void qc.invalidateQueries({ queryKey: conflictsKey })
      }
    },
  })

  // A "for all" action: one request for every pick, shown at the click and
  // undone field by field on a refusal, like a single pick.
  const batchMut = useMutation({
    mutationKey: [...resolutionMutationKey, 'batch'],
    meta: SILENT_ERROR_META,
    mutationFn: (picks: ResolveVars[]) =>
      planBranchesApi.saveResolutions(slug, branch.id, {
        resolutions: picks.map(({ entity_type, entity_name, field, choice }) => ({
          entity_type: entity_type as PlanDiffEntityType,
          entity_name,
          field_name: field,
          choice,
        })),
      }),
    onMutate: async (picks) => {
      await qc.cancelQueries({ queryKey: conflictsKey })
      const current = qc.getQueryData<PlanBranchConflicts>(conflictsKey)
      const previous = picks.map((pick) => ({
        ...pick,
        choice:
          current?.entities
            .find((e) => e.entity_type === pick.entity_type && e.name === pick.entity_name)
            ?.fields.find((f) => f.field === pick.field)?.choice ?? null,
      }))
      if (current) qc.setQueryData(conflictsKey, withChoices(current, picks))
      return { previous }
    },
    onError: (_error, _picks, context) => {
      const current = qc.getQueryData<PlanBranchConflicts>(conflictsKey)
      if (current && context) qc.setQueryData(conflictsKey, withChoices(current, context.previous))
    },
    onSettled: () => {
      if (qc.isMutating({ mutationKey: resolutionMutationKey }) === 1) {
        void qc.invalidateQueries({ queryKey: conflictsKey })
      }
    },
  })

  if (!open || !conflicts || conflicts.entities.length === 0) return null

  return (
    <Panel
      title="Conflicts"
      subtitle={`${conflicts.unresolved_count} unresolved`}
      subtitleTone={conflicts.unresolved_count > 0 ? 'danger' : 'neutral'}
    >
      <div className="space-y-3 p-4">
        <p className="text-caption text-fg-tertiary">
          Main and this branch both changed these since the branch was opened. Pick the value to
          keep for each, or for a whole entity at once; Update from main brings the rest of main
          in with your choices. Catalog position is never asked about: where both moved an item,
          main’s place stands.
        </p>
        <ConflictList
          entities={conflicts.entities}
          choiceOf={(_entity, field) => field.choice}
          pendingOf={(entity, field) =>
            savingFields.some(
              (vars) =>
                vars.entity_type === entity.entity_type &&
                vars.entity_name === entity.name &&
                vars.field === field.field,
            )
          }
          onResolve={
            canWrite
              ? (entity, field, choice) =>
                  resolutionMut.mutate({
                    entity_type: entity.entity_type,
                    entity_name: entity.name,
                    field: field.field,
                    choice,
                  })
              : undefined
          }
          onBulk={
            canWrite
              ? (strategy, scope) => {
                  const picks = bulkChoices(conflicts.entities, strategy, scope)
                  if (picks.length > 0) batchMut.mutate(picks)
                }
              : undefined
          }
          // A batch over a field whose own save is in flight would race it
          // to insert the same row.
          bulkPending={savingFields.length > 0}
        />
        {batchMut.isError ? (
          <p role="alert" className="text-caption text-danger">
            Could not save {countOf(batchMut.variables?.length ?? 0, 'choice', 'choices')}:{' '}
            {getErrorMessage(batchMut.error)}
          </p>
        ) : null}
        {resolutionMut.isError ? (
          <p role="alert" className="text-caption text-danger">
            {/* Named: other rows keep saving, so "the choice" alone may not be the last one clicked. */}
            Could not save the choice for {resolutionMut.variables?.entity_name}:{' '}
            {getErrorMessage(resolutionMut.error)}
          </p>
        ) : null}
      </div>
    </Panel>
  )
}

/** What each side did to the entity, for a presence row. */
function presenceText(entity: PlanBranchConflictEntity, field: PlanBranchConflictField) {
  const mainDeleted = field.ours === 'absent'
  const label = entityTypeTitle(entity.entity_type).toLowerCase()
  if (mainDeleted) {
    const dependents = field.dependents
    const parentWarning =
      dependents > 0
        ? ` Taking main also removes ${countOf(dependents, 'entity', 'entities')} this branch added or edited under it.`
        : ''
    return {
      summary: `Deleted on main · ${field.base === 'absent' ? 'added' : 'edited'} here`,
      consequence: {
        ours: `Take main deletes this ${label} on the branch.${parentWarning}`,
        theirs: `Keep this branch keeps it; the next merge adds it back to main.`,
      },
    }
  }
  return {
    summary: 'Edited on main · deleted here',
    consequence: {
      ours: `Take main restores this ${label} on the branch as main has it.`,
      theirs: 'Keep this branch leaves it deleted; the next merge deletes it on main.',
    },
  }
}

function ConflictFieldRow({
  entity,
  field,
  choice,
  pending,
  onResolve,
}: {
  entity: PlanBranchConflictEntity
  field: PlanBranchConflictField
  choice: ResolutionChoice | null
  pending: boolean
  /** Omitted for a viewer, who sees the three values but picks no side. */
  onResolve?: (choice: ResolutionChoice) => void
}) {
  const presence = field.field === PRESENCE_FIELD ? presenceText(entity, field) : null
  return (
    <div className="text-body-sm" data-unresolved={choice ? undefined : 'true'}>
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium text-fg">{presence ? presence.summary : field.field}</span>
        <span
          className="text-caption"
          style={{ color: choice ? 'var(--success)' : 'var(--danger)' }}
        >
          {choice ? CHOSEN_TEXT[choice] : 'Unresolved'}
        </span>
      </div>
      {presence ? (
        // A deletion has no value to print in three columns; what each
        // choice does is the useful thing to say.
        <ul className="mt-1 space-y-0.5 text-caption text-fg-tertiary">
          <li>{presence.consequence.ours}</li>
          <li>{presence.consequence.theirs}</li>
        </ul>
      ) : (
        // Stacked below `sm`: three monospace columns squeezed to ~100px each
        // on a phone. In time order, the two sides being chosen between last:
        // main when the branch opened, main now, this branch.
        <div className="mono mt-1 grid grid-cols-1 gap-1 sm:grid-cols-3 sm:gap-2">
          <ConflictValue label="Was (when the branch opened)" value={field.base} />
          <ConflictValue label="Main now" value={field.ours} />
          <ConflictValue label="This branch" value={field.theirs} />
        </div>
      )}
      {onResolve && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {(['ours', 'theirs'] as const).map((option) => (
            <Button
              key={option}
              type="button"
              size="sm"
              variant={choice === option ? 'default' : 'outline'}
              aria-pressed={choice === option}
              // The chosen side stays enabled: a disabled button loses its
              // fill, and every pick would look undone while the update runs.
              // Pressing it again picks what is already picked.
              disabled={pending && choice !== option}
              // Picking the side already picked changes nothing; sending it
              // would only race the save before it.
              onClick={() => {
                if (choice !== option) onResolve(option)
              }}
            >
              {CHOICE_LABEL[option]}
            </Button>
          ))}
        </div>
      )}
    </div>
  )
}

function ConflictValue({ label, value }: { label: string; value: unknown }) {
  // DiffValue, not `String(value ?? '∅')`: an empty string must read ∅ rather
  // than a blank cell, and collection fields (tags, field values, overrides)
  // arrive as structures now that every entity type reports its overlaps.
  //
  // No `table`: the three cells are peers in one grid row, and a table in one
  // of them would break the alignment.
  return (
    <div className="min-w-0">
      <span className="text-fg-tertiary">{label}: </span>
      <DiffValue value={value} />
    </div>
  )
}
