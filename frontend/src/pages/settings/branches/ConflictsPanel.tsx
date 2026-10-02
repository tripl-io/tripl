import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

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
  ResolutionChoice,
} from '@/types'
import { DiffValue } from '../DiffValue'
import { planBranchConflictsKey } from '@/lib/queryKeys'
import { entityTypeTitle } from './branchDiffModel'
import { withChoice } from './conflictModel'

/**
 * The backend's `ours` is main as it is now and `theirs` is this branch
 * (`plan_branch_conflicts.py`). Product users are not git users, so neither
 * word reaches the screen (PLAN-6). A stored choice names the resulting value,
 * so the same two words serve the merge and "Update from main" (PL-8).
 */
const CHOICE_LABEL: Record<ResolutionChoice, string> = {
  theirs: 'Keep this branch',
  ours: 'Take main',
}
const CHOSEN_TEXT: Record<ResolutionChoice, string> = {
  theirs: "Resolved: this branch's value",
  ours: "Resolved: main's value",
}

/** A presence row: one side deleted the entity (or its parent), the other
 * edited or added to it. Its values are "present" / "absent". */
const PRESENCE_FIELD = '@presence'

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
}

/**
 * Every overlap, grouped by entity type and parent ("Fields in checkout"),
 * each field with its three values and the two choices. Shared by the
 * Conflicts panel and the "Update from main" dialog, so a choice reads the
 * same in both (PL-8).
 */
export function ConflictList({ entities, choiceOf, onResolve, pending = false }: ConflictListProps) {
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
  return (
    <div className="space-y-3">
      {[...groups.entries()].map(([groupKey, group]) => (
        <section key={groupKey} className="space-y-2">
          <h3 className="text-caption font-medium text-fg-tertiary">{group.title}</h3>
          {group.entities.map((entity) => (
            <div
              // Two entity types may share a name; the type is part of the identity.
              key={`${entity.entity_type}:${entity.name}`}
              className="rounded-card border p-3 border-border-subtle"
            >
              {/* "Event type checkout", not the wire's `event_type: checkout` (PL-20). */}
              <div className="mb-1 text-body-sm text-fg-tertiary">
                {entityTypeTitle(entity.entity_type)}{' '}
                <span className="mono font-medium text-fg">{entity.label || entity.name}</span>
              </div>
              <div className="space-y-2">
                {entity.fields.map((field) => (
                  <ConflictFieldRow
                    key={field.field}
                    entity={entity}
                    field={field}
                    choice={choiceOf(entity, field)}
                    pending={pending}
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
  // resolve — so a merged or closed one never asks (PLAN-5).
  const open = branch.status !== 'merged' && branch.status !== 'closed'
  const conflictsKey = planBranchConflictsKey(slug, branch.id)
  const { data: conflicts } = useQuery({
    queryKey: conflictsKey,
    queryFn: () => planBranchesApi.getConflicts(slug, branch.id),
    enabled: open,
  })

  const resolutionMutationKey = ['plan-branch-resolution', slug, branch.id]
  const resolutionMut = useMutation({
    mutationKey: resolutionMutationKey,
    // Rendered inline below, beside the choice that failed (PLAN-7).
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
          keep for each; Update from main brings the rest of main in with your choices.
        </p>
        <ConflictList
          entities={conflicts.entities}
          choiceOf={(_entity, field) => field.choice}
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
        />
        {resolutionMut.isError ? (
          <p role="alert" className="text-caption text-danger">
            Could not save the choice: {getErrorMessage(resolutionMut.error)}
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
        // main when the branch opened, main now, this branch (PL-20).
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
              onClick={() => onResolve(option)}
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
