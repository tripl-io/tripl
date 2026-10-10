import type {
  PlanBranchConflictEntity,
  PlanBranchConflictField,
  PlanBranchConflicts,
  PlanDiffEntityType,
  ResolutionChoice,
} from '@/types'

/** A presence row: one side deleted the entity (or its parent), the other
 * edited or added to it. Its values are "present" / "absent". */
export const PRESENCE_FIELD = '@presence'

/** One field's choice, keyed the way the backend stores it. `null` clears it
 * (a rollback). */
export interface ConflictPick {
  entity_type: PlanDiffEntityType
  entity_name: string
  field: string
  choice: ResolutionChoice | null
}

/**
 * A "for all" action: keep this branch's value (`theirs`), take main's
 * (`ours`), or keep whichever side is filled in (`filled`).
 */
export type BulkStrategy = 'theirs' | 'ours' | 'filled'

/** Which entity a bulk action is limited to; omitted, it covers the list. */
export interface BulkScope {
  entity_type: string
  name: string
}

/**
 * Whether a conflict value counts as "not filled in" for `filled`: null,
 * undefined, '', [] and {}. Strings are not trimmed — whitespace is content.
 * The one definition there is: the strategy runs here, on the values the
 * response already carries, and the server only stores the picks.
 */
export function isEmptyConflictValue(value: unknown): boolean {
  if (value === null || value === undefined || value === '') return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value as object).length === 0
  return false
}

/** The side `filled` keeps: main's only where the branch's is empty and
 * main's is not; the branch's otherwise, both filled included. */
export function filledChoice(field: PlanBranchConflictField): ResolutionChoice {
  return isEmptyConflictValue(field.theirs) && !isEmptyConflictValue(field.ours)
    ? 'ours'
    : 'theirs'
}

function inScope(entity: PlanBranchConflictEntity, scope?: BulkScope): boolean {
  return !scope || (entity.entity_type === scope.entity_type && entity.name === scope.name)
}

/**
 * The picks a bulk action makes. Value rows only: a deletion (`@presence`)
 * takes dependents and cascades with it, so it stays a choice made row by
 * row. Picks already made in scope are overwritten — the user asked for all.
 */
export function bulkChoices(
  entities: readonly PlanBranchConflictEntity[],
  strategy: BulkStrategy,
  scope?: BulkScope,
): Array<ConflictPick & { choice: ResolutionChoice }> {
  return entities
    .filter((entity) => inScope(entity, scope))
    .flatMap((entity) =>
      entity.fields
        .filter((field) => field.field !== PRESENCE_FIELD)
        .map((field) => ({
          entity_type: entity.entity_type,
          entity_name: entity.name,
          field: field.field,
          choice: strategy === 'filled' ? filledChoice(field) : strategy,
        })),
    )
}

/** Presence rows in scope a bulk action leaves for the user to pick, by
 * whatever choice `choiceOf` shows for them now. */
export function presenceLeft(
  entities: readonly PlanBranchConflictEntity[],
  choiceOf: (
    entity: PlanBranchConflictEntity,
    field: PlanBranchConflictField,
  ) => ResolutionChoice | null,
  scope?: BulkScope,
): number {
  return entities
    .filter((entity) => inScope(entity, scope))
    .reduce(
      (count, entity) =>
        count +
        entity.fields.filter(
          (field) => field.field === PRESENCE_FIELD && choiceOf(entity, field) === null,
        ).length,
      0,
    )
}

function pickKey(entityType: string, entityName: string, field: string): string {
  return `${entityType}\u0000${entityName}\u0000${field}`
}

/**
 * The conflicts with each pick's choice set (or cleared, for a rollback), and
 * the unresolved count recounted to match. A new object: the cache entry is
 * never edited in place.
 */
export function withChoices(
  conflicts: PlanBranchConflicts,
  picks: readonly ConflictPick[],
): PlanBranchConflicts {
  const byKey = new Map(
    picks.map((pick) => [pickKey(pick.entity_type, pick.entity_name, pick.field), pick.choice]),
  )
  const entities = conflicts.entities.map((entity) => {
    const keyOf = (field: PlanBranchConflictField) =>
      pickKey(entity.entity_type, entity.name, field.field)
    if (!entity.fields.some((field) => byKey.has(keyOf(field)))) return entity
    return {
      ...entity,
      fields: entity.fields.map((field) =>
        byKey.has(keyOf(field)) ? { ...field, choice: byKey.get(keyOf(field)) ?? null } : field,
      ),
    }
  })
  const unresolved_count = entities.reduce(
    (count, entity) => count + entity.fields.filter((field) => field.choice == null).length,
    0,
  )
  return { ...conflicts, entities, unresolved_count }
}

/** `withChoices` for one field. */
export function withChoice(
  conflicts: PlanBranchConflicts,
  target: Omit<ConflictPick, 'choice'>,
  choice: ResolutionChoice | null,
): PlanBranchConflicts {
  return withChoices(conflicts, [{ ...target, choice }])
}
