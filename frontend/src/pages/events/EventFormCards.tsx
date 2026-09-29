/**
 * The sections of the single-event form below Details, split out of
 * `EventForm.tsx` along the seams the form already had (EVT-30). State stays
 * with the form — a save reads all of it — and each card renders one part.
 */
import type { Event as TEvent, FieldDefinition, MetaFieldDefinition, Variable } from '@/types'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Check, Plus, X } from 'lucide-react'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import type { ScenarioStepId } from '@/demo/scenarioModel'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { FieldError } from '@/components/forms/FieldError'
import { EvField, EvInput, SurfCard } from './eventFormLayout'
import {
  FieldBreakdownLink,
  FieldTemplateHints,
  FieldValueControl,
  MetaFieldControl,
  ScanMaintenanceNotice,
} from './eventFormFields'
import { breakdownChipId, focusBreakdownChip } from './eventFormValues'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export function TagsBreakdownsCard({
  tags,
  onTagsChange,
  tagInput,
  onTagInputChange,
  onCommitTag,
  breakdownOptions,
  breakdownColumns,
  onToggleBreakdown,
  breakdownInput,
  onBreakdownInputChange,
  onCommitBreakdown,
}: {
  tags: string[]
  onTagsChange: (tags: string[]) => void
  tagInput: string
  onTagInputChange: (value: string) => void
  /** Adds what is in the tag input as a chip and clears the input. */
  onCommitTag: () => void
  breakdownOptions: string[]
  breakdownColumns: string[]
  onToggleBreakdown: (column: string) => void
  breakdownInput: string
  onBreakdownInputChange: (value: string) => void
  /** Adds what is in the column input (never removes) and clears it. */
  onCommitBreakdown: () => void
}) {
  return (
    <SurfCard
      title="Tags & breakdowns"
      // What each half is for, which the form never said (AU-22).
      subtitle="Tags are labels for finding events in the list; breakdowns decide which columns metrics are split by."
    >
      <EvField label="Tags" htmlFor="form-tags" hint="Press Enter or comma to add. Anything left typed is added on save.">
        {tags.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-[6px]">
            {tags.map(t => (
              // Taller on a phone, with a 28px remove target: an 11px icon in a
              // 22px chip could not be hit with a finger (AU-39).
              <span
                key={t}
                className="inline-flex h-[22px] items-center gap-[5px] rounded-full pl-[9px] pr-[6px] text-caption max-sm:h-8 bg-surface-hover"
              >
                {t}
                <button
                  type="button"
                  onClick={() => onTagsChange(tags.filter(x => x !== t))}
                  className="grid place-items-center rounded-full transition-colors hover:text-[var(--danger)] max-sm:-mr-1 max-sm:size-7"
                  style={{ color: 'var(--fg-subtle)' }}
                  aria-label={`Remove ${t} tag`}
                >
                  <X className="size-3" aria-hidden="true" />
                </button>
              </span>
            ))}
          </div>
        )}
        <EvInput
          id="form-tags"
          value={tagInput}
          onChange={e => onTagInputChange(e.target.value)}
          onKeyDown={e => {
            if ((e.key === 'Enter' || e.key === ',') && tagInput.trim()) {
              e.preventDefault()
              onCommitTag()
            }
          }}
          // A chip typed and then left used to vanish on save (EVT-26).
          onBlur={() => { if (tagInput.trim()) onCommitTag() }}
          placeholder="Type tag + Enter"
        />
      </EvField>

      <EvField
        label="Metric breakdowns"
        htmlFor="form-breakdown-column"
        hint="Warehouse columns to roll metrics up by. Click a column to toggle it; type any other below."
        last
      >
        {/* Toggles, and drawn as toggles (AU-23): outlined grey pills with
            nothing on them read as read-only tags or examples until one turned
            teal. A leading check when on and a plus when off say "click me". */}
        <div className="flex flex-wrap gap-[6px]" role="group" aria-label="Suggested breakdown columns">
          {breakdownOptions.map(c => {
            const on = breakdownColumns.includes(c)
            const Icon = on ? Check : Plus
            return (
              <button
                key={c}
                id={breakdownChipId(c)}
                type="button"
                aria-pressed={on}
                onClick={() => onToggleBreakdown(c)}
                className="mono inline-flex items-center gap-1 rounded-full py-1 pl-[7px] pr-[9px] text-caption transition-colors hover:border-[var(--accent)]"
                style={{
                  border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                  background: on ? 'var(--accent-soft)' : 'var(--bg)',
                  color: on ? 'var(--accent)' : 'var(--fg-muted)',
                }}
              >
                <Icon className="size-3" aria-hidden="true" />
                {c}
              </button>
            )
          })}
        </div>
        {/* The other half of what the docs describe, and what the redesign
            dropped: a column the offered set cannot know about — one the
            base query computes, or a scan column no field definition
            covers — is still reachable by typing it. */}
        <EvInput
          id="form-breakdown-column"
          className="mono mt-2"
          value={breakdownInput}
          onChange={e => onBreakdownInputChange(e.target.value)}
          onKeyDown={e => {
            if ((e.key === 'Enter' || e.key === ',') && breakdownInput.trim()) {
              e.preventDefault()
              onToggleBreakdown(breakdownInput.trim())
              onBreakdownInputChange('')
            }
          }}
          onBlur={() => { if (breakdownInput.trim()) onCommitBreakdown() }}
          placeholder="Other column + Enter"
        />
      </EvField>
    </SurfCard>
  )
}

export function FieldValuesCard({
  slug,
  event,
  fields,
  typeLabel,
  eventTypeId,
  nameFormat,
  namingColumns,
  fieldValues,
  onFieldValueChange,
  variables,
  storedFieldValues,
  collectedBreakdownColumns,
  breakdownColumns,
  coachStep,
  coachActive,
  errors = {},
}: {
  slug: string
  event: TEvent | null
  fields: FieldDefinition[]
  typeLabel: string
  /** The selected type, for the subtitle's link to its fields. */
  eventTypeId?: string
  nameFormat: string | null
  namingColumns: ReadonlySet<string>
  fieldValues: Record<string, string>
  onFieldValueChange: (fieldId: string, value: string) => void
  variables: Variable[]
  storedFieldValues: ReadonlyMap<string, { value: string; isAuthored: boolean }>
  collectedBreakdownColumns: ReadonlySet<string>
  breakdownColumns: string[]
  coachStep: ScenarioStepId
  coachActive: boolean
  /** Messages the form shows under a row after a refused Save, keyed by the
   *  control id (`field-<id>`): "Required" for an empty required value. */
  errors?: Record<string, string>
}) {
  const branchId = useActiveBranchId()
  const branchLink = useBranchLinkProps()
  if (fields.length === 0) return null
  return (
    <SurfCard
      title="Field values"
      subtitle={
        // What these are and where they come from (AU-22): the columns a scan
        // matches the event on, defined by the type, and that `${` opens the
        // variable list — which was only discoverable by typing it.
        <>
          Columns defined by the{' '}
          {eventTypeId ? (
            <SubtitleLink to={projectPath(currentOrgSlug(), slug, `/event-types/${eventTypeId}`)}>{typeLabel}</SubtitleLink>
          ) : (
            typeLabel
          )}{' '}
          type; scans match the event on these.
          {nameFormat ? ` The event name is built from ${[...namingColumns].join(', ')}.` : ''}{' '}
          Type <span className="mono">{'${'}</span> to insert a property.
        </>
      }
    >
      {fields.map((f, i) => {
        const namesEvent = namingColumns.has(f.name)
        // A naming row is required in practice whatever its schema says:
        // Create stays blocked until it is filled. Marking only `is_required`
        // made the form's own marks disagree with what it enforces — on
        // acme-ios's `se` type none of the three columns that build the name
        // carries the flag (tripl-u2h9.4).
        const required = f.is_required || namesEvent
        const value = fieldValues[f.id] ?? ''
        const inputId = `field-${f.id}`
        const error = errors[inputId]
        return (
          <EvField
            key={f.id}
            label={f.display_name}
            htmlFor={`field-${f.id}`}
            required={required}
            hint={
              <>
                <span className="mono">{f.name} · {f.field_type}</span>
                {namesEvent && (
                  <span className="mt-[2px] block text-accent">
                    names the event
                  </span>
                )}
              </>
            }
            last={i === fields.length - 1}
            notes={
              <>
                <FieldError inputId={inputId} message={error} />
                <FieldTemplateHints value={value} variables={variables} namesEvent={namesEvent} slug={slug} />
                <ScanMaintenanceNotice
                  stored={storedFieldValues.get(f.id) ?? null}
                  current={value}
                  onHandBack={required ? undefined : () => onFieldValueChange(f.id, '')}
                />
                {/* A JSON field is not a warehouse column, so it can never be a
                    breakdown — `breakdownOptions` leaves those out too. */}
                {event && f.field_type !== 'json' && (
                  <FieldBreakdownLink
                    column={f.name}
                    href={branchLink(
                      projectPath(currentOrgSlug(), slug, `/monitoring/event/${event.id}`)
                        + `?tab=breakdowns&column=${encodeURIComponent(f.name)}`,
                      branchId,
                    )}
                    state={
                      collectedBreakdownColumns.has(f.name)
                        ? 'collected'
                        : breakdownColumns.includes(f.name)
                          ? 'collecting'
                          : 'off'
                    }
                    onShowBreakdowns={() => focusBreakdownChip(f.name)}
                  />
                )}
              </>
            }
          >
            {/* The div is the mark's anchor: FieldValueControl is a plain
                function component and would swallow the cloned ref. */}
            <ScenarioCoachMark step={coachStep} when={f.name === SCENARIO_SEEDED.editedFieldName}>
              <div
                className={
                  coachActive && f.name === SCENARIO_SEEDED.editedFieldName ? 'max-w-[320px]' : undefined
                }
              >
                <FieldValueControl
                  field={f}
                  // The label's required mark and the control must agree: a
                  // naming column IS required to create the event, whatever
                  // its schema flag says, and marking one without the other
                  // is how the form came to promise a check nothing ran.
                  requiredOverride={required}
                  inputId={inputId}
                  invalid={!!error}
                  value={value}
                  onChange={v => onFieldValueChange(f.id, v)}
                  variables={variables}
                />
              </div>
            </ScenarioCoachMark>
          </EvField>
        )
      })}
    </SurfCard>
  )
}

export function MetaFieldsCard({
  slug,
  metaFields,
  metaValues,
  onMetaValuesChange,
  variables,
}: {
  /** For the subtitle's link to where meta fields are defined. */
  slug?: string
  metaFields: MetaFieldDefinition[]
  metaValues: Record<string, string[]>
  onMetaValuesChange: (metaFieldId: string, values: string[]) => void
  variables: Variable[]
}) {
  if (metaFields.length === 0) return null
  return (
    <SurfCard
      title="Meta fields"
      // How these differ from the type's field values (AU-22, JR-30): the
      // same project-wide set on every event, for people rather than scans.
      subtitle={
        <>
          Project-wide attributes for people, not scans — owner team, ticket links.
          {slug && (
            <>
              {' '}
              <SubtitleLink to={projectPath(currentOrgSlug(), slug, '/meta-fields')}>Manage meta fields</SubtitleLink>
            </>
          )}
        </>
      }
    >
      {metaFields.map((mf, i) => (
        <EvField
          key={mf.id}
          label={mf.display_name}
          htmlFor={`meta-${mf.id}`}
          required={mf.is_required}
          last={i === metaFields.length - 1}
        >
          <MetaFieldControl
            metaField={mf}
            inputId={`meta-${mf.id}`}
            values={metaValues[mf.id] ?? []}
            onChange={next => onMetaValuesChange(mf.id, next)}
            variables={variables}
          />
        </EvField>
      ))}
    </SurfCard>
  )
}

/** A link inside a card subtitle: the subtitle's own size, underlined. */
function SubtitleLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="underline underline-offset-2 hover:text-[var(--fg)]">
      {children}
    </Link>
  )
}
