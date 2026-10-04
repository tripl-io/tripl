import { countOf } from '@/lib/plural'
import type { MetaFieldUsage } from '@/types'

/**
 * The delete confirm's sentence: counted when the usage is known,
 * named when it is not, and honest when nothing holds a value.
 */
export function metaFieldDeleteMessage(displayName: string, usage: MetaFieldUsage | null): string {
  if (usage === null) {
    return `Removes every ${displayName} value from the events that carry one. This can't be undone.`
  }
  if (usage.value_count === 0) {
    return `No event holds a ${displayName} value, so only the field itself is removed. This can't be undone.`
  }
  return `Removes ${countOf(usage.value_count, `${displayName} value`, `${displayName} values`)} from ${countOf(usage.event_count, 'event', 'events')}. This can't be undone.`
}
