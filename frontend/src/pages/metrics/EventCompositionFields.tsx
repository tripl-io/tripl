import { useQuery } from '@tanstack/react-query'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { SCard, NativeSelect, TextInput, Field } from '@/components/settings/kit'
import { eventKey, eventTypesKey } from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventNameLabel } from '@/lib/eventName'
import { METRIC_COMPOSITIONS, type EventType, type MetricComposition } from '@/types'
import { EventRefPicker, type EventRef } from './EventRefPicker'
import { errorAria, type FieldErrors } from '@/lib/fieldErrors'
import type { MetricDraft } from './metricDraft'
import { examplePlaceholder } from '@/components/forms/placeholders'

// What each composition calculates, in words; the raw option values
// are kept as-is on the wire.
const COMPOSITION_LABEL: Record<MetricComposition, string> = {
  single: 'Count of an event',
  ratio: 'Ratio of two events (A ÷ B)',
  per_distinct_user: 'Per user (events ÷ distinct users)',
}

/**
 * The display name of what one side counts, for the formula line: the event
 * (read from the cache the picker fills, same key) or "every <type> event".
 */
function useEventRefName(slug: string, ref: EventRef, eventTypes: readonly EventType[]): string | null {
  const eventQuery = useQuery({
    queryKey: eventKey(slug, null, ref.eventId),
    queryFn: () => eventsApi.get(slug, ref.eventId),
    enabled: !!ref.eventId,
    meta: SILENT_ERROR_META,
  })
  if (ref.eventId) return eventQuery.data ? eventNameLabel(eventQuery.data.name) : null
  const type = eventTypes.find(candidate => candidate.id === ref.eventTypeId)
  return type ? `every ${type.display_name} event` : null
}

const EMPTY_EVENT_TYPES: EventType[] = []

interface EventCompositionFieldsProps {
  slug: string
  draft: MetricDraft
  patch: (next: Partial<MetricDraft>) => void
  errors: FieldErrors
  disabled?: boolean
}

/** The event-composition card: composition, the event(s) it counts, user id column. */
export function EventCompositionFields({
  slug,
  draft,
  patch,
  errors,
  disabled,
}: EventCompositionFieldsProps) {
  // Event types are a short, unpaginated list; both pickers share this cache.
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, null),
    queryFn: () => eventTypesApi.list(slug),
    // Types are an optional second group in the pickers; a failure leaves the
    // events group working and is not worth a toast.
    meta: SILENT_ERROR_META,
  })
  const eventTypes = eventTypesQuery.data ?? EMPTY_EVENT_TYPES
  const isRatio = draft.composition === 'ratio'

  const numerator: EventRef = {
    eventId: draft.numeratorEventId,
    eventTypeId: draft.numeratorEventTypeId,
  }
  const denominator: EventRef = {
    eventId: draft.denominatorEventId,
    eventTypeId: draft.denominatorEventTypeId,
  }
  const numeratorName = useEventRefName(slug, numerator, eventTypes)
  const denominatorName = useEventRefName(slug, isRatio ? denominator : { eventId: '', eventTypeId: '' }, eventTypes)
  // One line saying what the metric will compute, so a swapped numerator and
  // denominator is visible before saving.
  const formula = !numeratorName
    ? null
    : draft.composition === 'ratio'
      ? denominatorName
        ? `${numeratorName} ÷ ${denominatorName}`
        : null
      : draft.composition === 'per_distinct_user'
        ? `${numeratorName} ÷ distinct users`
        : `Count of ${numeratorName}`

  return (
    <SCard title="Events" description="Count tracked events, or divide one by another.">
      {/* "Calculate", not "Composition": the kind already says where the
          value comes from, this says how. */}
      <Field label="Calculate" htmlFor="metric-composition" required>
        <NativeSelect
          id="metric-composition"
          value={draft.composition}
          onChange={value => patch({ composition: value as MetricComposition })}
          options={METRIC_COMPOSITIONS.map(c => ({ value: c, label: COMPOSITION_LABEL[c] }))}
        />
      </Field>
      <Field
        label={isRatio ? 'Numerator event' : 'Event'}
        htmlFor="metric-numerator"
        required
        last={draft.composition === 'single'}
        hint="An event, or an event type to count every event of that type."
        error={errors['metric-numerator']}
        announceError={false}
      >
        <EventRefPicker
          slug={slug}
          id="metric-numerator"
          label={isRatio ? 'numerator events' : 'events'}
          value={numerator}
          onChange={next =>
            patch({ numeratorEventId: next.eventId, numeratorEventTypeId: next.eventTypeId })
          }
          eventTypes={eventTypes}
          disabled={disabled}
          {...errorAria(errors, 'metric-numerator')}
        />
      </Field>
      {isRatio && (
        <Field
          label="Denominator event"
          htmlFor="metric-denominator"
          required
          last
          hint="Required for a ratio metric."
          error={errors['metric-denominator']}
          announceError={false}
        >
          <EventRefPicker
            slug={slug}
            id="metric-denominator"
            label="denominator events"
            value={denominator}
            onChange={next =>
              patch({ denominatorEventId: next.eventId, denominatorEventTypeId: next.eventTypeId })
            }
            eventTypes={eventTypes}
            disabled={disabled}
            {...errorAria(errors, 'metric-denominator')}
          />
        </Field>
      )}
      {draft.composition === 'per_distinct_user' && (
        <Field
          label="User ID column"
          htmlFor="metric-user-id-column"
          last
          hint="Column counted for distinct users. Defaults to user_id."
        >
          <div className="max-w-[280px]">
            <TextInput
              id="metric-user-id-column"
              value={draft.userIdColumn}
              onChange={value => patch({ userIdColumn: value })}
              mono
              placeholder={examplePlaceholder('user_id')}
            />
          </div>
        </Field>
      )}
      {formula && (
        <p
          className="border-t px-4 py-[11px] text-body-sm border-border-subtle text-fg-secondary"
        >
          Computes: <span className="text-fg">{formula}</span>
        </p>
      )}
    </SCard>
  )
}
