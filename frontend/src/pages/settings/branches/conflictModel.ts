import type { PlanBranchConflicts, ResolutionChoice } from '@/types'

/**
 * The conflicts with one field's choice set (or cleared, for a rollback), and
 * the unresolved count recounted to match. A new object: the cache entry is
 * never edited in place.
 */
export function withChoice(
  conflicts: PlanBranchConflicts,
  target: { entity_type: string; entity_name: string; field: string },
  choice: ResolutionChoice | null,
): PlanBranchConflicts {
  const entities = conflicts.entities.map((entity) =>
    entity.entity_type === target.entity_type && entity.name === target.entity_name
      ? {
          ...entity,
          fields: entity.fields.map((field) =>
            field.field === target.field ? { ...field, choice } : field,
          ),
        }
      : entity,
  )
  const unresolved_count = entities.reduce(
    (count, entity) => count + entity.fields.filter((field) => field.choice === null).length,
    0,
  )
  return { ...conflicts, entities, unresolved_count }
}
