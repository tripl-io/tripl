import { useId, type ReactNode } from 'react'
import type { OrgSettings, OrgSettingSource } from '@/api/orgSettings'
import { Chip, type ChipTone, type ChipVariant } from '@/components/primitives/chip'
import { Field, TextInput } from '@/components/settings/kit'
import {
  type DraftValue,
  type OrgDraft,
  type OrgSection,
  SECRET_FIELDS,
  displayValue,
  inheritedValue,
  isOwnValue,
  numberError,
  savedValue,
  sourceOf,
} from './orgSettingsModel'

const SOURCE_BADGE: Record<
  Exclude<OrgSettingSource, 'default'>,
  { label: string; tone: ChipTone; variant: ChipVariant; title: string }
> = {
  org: {
    label: 'Organization',
    tone: 'accent',
    variant: 'soft',
    title: "This organization's own value.",
  },
  override: {
    label: 'Operator',
    tone: 'info',
    variant: 'soft',
    title: "Set by the platform operator. Organizations without a value of their own use it.",
  },
  env: {
    label: 'Env',
    tone: 'neutral',
    variant: 'outline',
    title: "Delivered to the server by an environment variable, differing from the built-in default.",
  },
  disabled: {
    label: 'Disabled by operator policy',
    tone: 'warning',
    variant: 'soft',
    title:
      "The operator does not share this with organizations (ORG_SETTINGS_OPERATOR_FALLBACK=none). Set this organization's own to use it.",
  },
}

/**
 * Where an organization's value came from. No badge means the built-in
 * default, the same rule the Platform pages use.
 */
export function OrgSourceBadge({ source }: { source: OrgSettingSource }) {
  if (source === 'default') return null
  const { label, tone, variant, title } = SOURCE_BADGE[source]
  return (
    <Chip tone={tone} variant={variant} size="xs" title={title}>
      {label}
    </Chip>
  )
}

export const ORG_SOURCE_LEGEND =
  "Organization: this organization's own value. Operator: the platform's value, used while the organization has none. Env: the server's environment. No badge: the built-in default."

export type OrgFieldProps = {
  settings: OrgSettings
  draft: OrgDraft
  setField: (field: string, value: DraftValue) => void
}

function formatInherited(value: string | number | boolean): string {
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (typeof value === 'number') return value.toLocaleString('en-US')
  if (value === '') return 'empty'
  // A system prompt runs to paragraphs; its opening says which one it is.
  return value.length > 60 ? `${value.slice(0, 60)}…` : value
}

/**
 * The line under a field the organization has its own value for: what it
 * would get back, and the control that clears its value. Grouped fields
 * (endpoint and credential) are cleared together, from the card, so they get
 * no per-field control.
 */
export function InheritHint({
  settings,
  draft,
  section,
  field,
  setField,
  grouped = false,
}: OrgFieldProps & { section: OrgSection; field: string; grouped?: boolean }) {
  if (settings.scope !== 'organization') return null
  if (draft[field] === null) {
    return (
      <span>
        Saving clears this organization's value.{' '}
        <button
          type="button"
          className="font-medium text-accent hover:underline"
          onClick={() => setField(field, SECRET_FIELDS.has(field) ? '' : String(savedValue(settings, section, field)))}
        >
          Keep it
        </button>
      </span>
    )
  }
  if (!isOwnValue(settings, section, field) || grouped || SECRET_FIELDS.has(field)) return null
  const inherited = inheritedValue(settings, section, field)
  return (
    <span>
      Without it: {formatInherited(inherited)}.{' '}
      <button
        type="button"
        className="font-medium text-accent hover:underline"
        onClick={() => setField(field, null)}
      >
        Use the inherited value
      </button>
    </span>
  )
}

/** A text or number field of the organization's, badged with its source. */
export function OrgTextField({
  settings,
  draft,
  setField,
  section,
  field,
  label,
  hint,
  placeholder,
  number = false,
  suffix,
  grouped,
  last,
}: OrgFieldProps & {
  section: OrgSection
  field: string
  label: string
  hint?: ReactNode
  placeholder?: string
  number?: boolean
  suffix?: string
  grouped?: boolean
  last?: boolean
}) {
  const errorId = useId()
  const value = displayValue(settings, draft, section, field)
  const error = number && field in draft ? numberError(field, draft[field] ?? null, settings) : null
  const inherit = (
    <InheritHint
      settings={settings}
      draft={draft}
      section={section}
      field={field}
      setField={setField}
      grouped={grouped}
    />
  )
  return (
    <Field
      label={label}
      labelRight={<OrgSourceBadge source={sourceOf(settings, section, field)} />}
      hint={
        hint || settings.scope === 'organization' ? (
          <>
            {hint}
            {hint ? ' ' : null}
            {inherit}
          </>
        ) : undefined
      }
      last={last}
    >
      <TextInput
        type={number ? 'number' : 'text'}
        value={String(value)}
        onChange={next => setField(field, next)}
        placeholder={placeholder}
        suffix={suffix}
        mono
        aria-invalid={error !== null}
        aria-describedby={error ? errorId : undefined}
      />
      {error && (
        <p id={errorId} className="mt-1 text-caption text-danger">
          {error}
        </p>
      )}
    </Field>
  )
}
