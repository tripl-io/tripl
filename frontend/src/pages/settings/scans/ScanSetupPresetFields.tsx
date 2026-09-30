import type { EventType, ScanConfigPreview, ScanPreviewEventProperties, ScanSetupPreset } from '@/types'
import { Field, NativeSelect } from '@/components/settings/kit'
import { CodeToken } from '@/components/primitives/code-token'
import { FieldError } from '@/components/forms/FieldError'
import { invalidAria } from '@/components/forms/validation'
import { countOf } from '@/lib/plural'
import {
  PRESET_EVENT_TYPE_LABEL,
  SETUP_PRESET_OPTIONS,
  eventPropertiesFor,
  jsonColumnNames,
  scalarColumnNames,
} from './scanSetupPreset'

/**
 * "How are events laid out in this table?" — the Event + properties preset, or
 * the custom setup. Radio cards like the mode choice above it, so the two
 * questions read as one family.
 */
export function SetupPresetChoice({
  value,
  onChange,
}: {
  value: ScanSetupPreset
  onChange: (value: ScanSetupPreset) => void
}) {
  return (
    <fieldset data-testid="scan-setup-preset" className="border-b px-4 py-4 border-border-subtle">
      <legend className="float-left mb-2 w-full text-body font-medium text-fg">
        How events are stored
      </legend>
      <div className="clear-both flex flex-col gap-2">
        {SETUP_PRESET_OPTIONS.map(option => (
          <div
            key={option.value}
            className="flex items-start gap-2.5 rounded-lg border p-3"
            style={{
              borderColor: value === option.value ? 'var(--accent)' : 'var(--border-subtle)',
            }}
          >
            <input
              type="radio"
              id={`scan-setup-${option.value}`}
              name="scan-setup-preset"
              className="mt-0.5"
              value={option.value}
              checked={value === option.value}
              aria-describedby={`scan-setup-${option.value}-description`}
              onChange={() => onChange(option.value)}
            />
            <div className="min-w-0">
              <label
                htmlFor={`scan-setup-${option.value}`}
                className="block text-body font-medium text-fg"
              >
                {option.label}
              </label>
              <p
                id={`scan-setup-${option.value}-description`}
                className="mt-0.5 text-body-sm leading-snug text-fg-tertiary"
              >
                {option.description}
              </p>
            </div>
          </div>
        ))}
      </div>
    </fieldset>
  )
}

/**
 * The preset's questions: which column names the event, which JSON column holds
 * its properties, and which event type files them. Everything the custom setup
 * asks on top (name format, JSON value paths, Event type column, group rules)
 * the backend derives from these.
 */
export function PresetColumnFields({
  preview,
  eventTypes,
  jsonStringColumns = [],
  summaryStale = false,
  eventNameColumn,
  propertiesColumn,
  eventTypeId,
  onEventNameColumnChange,
  onPropertiesColumnChange,
  onEventTypeIdChange,
}: {
  preview: ScanConfigPreview | null
  eventTypes: EventType[]
  /** Text columns ticked under "Parse as JSON": properties columns too (F23.9). */
  jsonStringColumns?: string[]
  /** The preview was loaded with other columns parsed; its summary is withheld. */
  summaryStale?: boolean
  eventNameColumn: string
  propertiesColumn: string
  eventTypeId: string
  onEventNameColumnChange: (value: string) => void
  onPropertiesColumnChange: (value: string) => void
  onEventTypeIdChange: (value: string) => void
}) {
  // A saved config opens before any preview: its columns stay among the
  // choices, as the other column pickers on this form do.
  const withSaved = (saved: string, choices: string[]) =>
    saved && !choices.includes(saved) ? [saved, ...choices] : choices
  const eventChoices = withSaved(eventNameColumn, scalarColumnNames(preview, jsonStringColumns))
  const jsonChoices = jsonColumnNames(preview, jsonStringColumns)
  const propertiesChoices = withSaved(propertiesColumn, jsonChoices)
  const noJsonColumn = Boolean(preview) && jsonChoices.length === 0
  const summary = summaryStale ? null : eventPropertiesFor(preview, eventNameColumn, propertiesColumn)

  return (
    <>
      <Field
        label="Event column"
        htmlFor="scan-event-name-column"
        hint="The column holding each row's event name. Every distinct value becomes one event."
      >
        <NativeSelect
          id="scan-event-name-column"
          value={eventNameColumn}
          onChange={onEventNameColumnChange}
          disabled={!preview}
          {...invalidAria('scan-event-name-column', Boolean(preview) && !eventNameColumn)}
          options={[
            { value: '', label: preview ? 'Choose a column' : 'Load preview first', disabled: true },
            ...eventChoices,
          ]}
        />
        <FieldError
          inputId="scan-event-name-column"
          announce
          message={preview && !eventNameColumn ? 'Pick the column your event names are in.' : null}
        />
      </Field>
      <Field
        label="Properties column"
        htmlFor="scan-properties-column"
        hint="The JSON column holding each event's properties, or a text column ticked under Parse as JSON. Every key becomes a property of the event, typed from its values and with how often the event carries it."
      >
        <NativeSelect
          id="scan-properties-column"
          value={propertiesColumn}
          onChange={onPropertiesColumnChange}
          disabled={!preview || noJsonColumn}
          {...invalidAria('scan-properties-column', Boolean(preview) && !propertiesColumn)}
          options={[
            {
              value: '',
              label: preview ? (noJsonColumn ? 'No JSON column in this query' : 'Choose a JSON column') : 'Load preview first',
              disabled: true,
            },
            ...propertiesChoices,
          ]}
        />
        {/* JSON-typed columns, and the text columns ticked under "Parse as
            JSON": a scan reads only those as JSON. */}
        <FieldError
          inputId="scan-properties-column"
          announce
          message={
            noJsonColumn
              ? 'This query returns no JSON column. Tick its text column under Parse as JSON, select the properties column in the query, or use the custom setup.'
              : preview && !propertiesColumn
                ? 'Pick the JSON column your event properties are in.'
                : null
          }
        />
      </Field>
      <Field
        label="Event type"
        htmlFor="scan-preset-event-type"
        hint="The folder these events are filed under."
      >
        <NativeSelect
          id="scan-preset-event-type"
          value={eventTypeId}
          onChange={onEventTypeIdChange}
          options={[
            { value: '', label: PRESET_EVENT_TYPE_LABEL },
            ...eventTypes.map(et => ({ value: et.id, label: et.display_name })),
          ]}
        />
      </Field>
      {preview && eventNameColumn && propertiesColumn && (
        <div className="border-b px-4 py-4 border-border-subtle">
          <EventPropertiesPreview summary={summary} />
        </div>
      )}
    </>
  )
}

function percent(share: number): string {
  return `${Math.round(share * 100)}%`
}

/**
 * What the preset makes of the preview's sample: the events, and each one's
 * properties with how often it carried them and the type a run would set.
 */
export function EventPropertiesPreview({ summary }: { summary: ScanPreviewEventProperties | null }) {
  if (!summary) {
    return (
      <p className="text-body-sm text-fg-tertiary" data-testid="event-properties-preview">
        Reload the preview to see the events and properties these columns give.
      </p>
    )
  }
  if (summary.error) {
    return (
      <p className="text-body-sm text-danger" role="alert" data-testid="event-properties-preview">
        {summary.error}
      </p>
    )
  }
  if (summary.events.length === 0) {
    return (
      <p className="text-body-sm text-fg-tertiary" data-testid="event-properties-preview">
        No event names in the sample rows.
      </p>
    )
  }
  return (
    <div data-testid="event-properties-preview" className="space-y-3">
      <p className="m-0 text-body-sm text-fg-secondary">
        In {countOf(summary.sample_rows, 'sample row', 'sample rows')}:{' '}
        {countOf(summary.events.length, 'event', 'events')}. A sample — the check below reads the whole lookback window.
      </p>
      <ul className="m-0 list-none space-y-2 p-0">
        {summary.events.map(event => (
          <li key={event.name} className="rounded-md border border-border-subtle px-3 py-2">
            <div className="flex items-baseline gap-2">
              <CodeToken>{event.name}</CodeToken>
              <span className="text-caption text-fg-tertiary">
                {countOf(event.sample_rows, 'row', 'rows')} ·{' '}
                {countOf(event.properties.length, 'property', 'properties')}
              </span>
            </div>
            {event.properties.length > 0 && (
              <ul className="m-0 mt-1.5 list-none space-y-0.5 p-0 text-caption">
                {event.properties.map(property => (
                  <li key={property.path} className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-mono text-fg">{property.path}</span>
                    <span className="text-fg-tertiary">{property.type ?? 'type unknown'}</span>
                    <span className="text-fg-tertiary">in {percent(property.presence)}</span>
                    {property.sample_values.length > 0 && (
                      <span className="truncate text-fg-subtle">
                        e.g. {property.sample_values.join(', ')}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
