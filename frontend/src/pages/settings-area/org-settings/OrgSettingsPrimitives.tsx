import { useId, type ReactNode } from 'react'
import type { OrgSettings, OrgSettingSource } from '@/api/orgSettings'
import { Chip, type ChipTone, type ChipVariant } from '@/components/primitives/chip'
import { Field, TextInput } from '@/components/settings/kit'
import { ReadOnlyValue, SourceBadge } from '@/pages/settings-service/ServiceSettingsPrimitives'
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
import { formatNumber } from '@/lib/format'

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
  // "Platform", the rail's name for the layer above every organization: the
  // value set under Settings › Platform.
  override: {
    label: 'Platform',
    tone: 'info',
    variant: 'soft',
    title: 'Set under Settings › Platform. Organizations without a value of their own use it.',
  },
  env: {
    label: 'Env',
    tone: 'neutral',
    variant: 'outline',
    title: "Delivered to the server by an environment variable, differing from the built-in default.",
  },
  disabled: {
    label: 'Not shared by the platform',
    tone: 'warning',
    variant: 'soft',
    title:
      "The platform does not share this with organizations (ORG_SETTINGS_OPERATOR_FALLBACK=none). Set this organization's own to use it.",
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

/**
 * The badge of one field, in its scope's own words. An organization's page
 * says where an inherited value comes from (Organization, Platform, Env). The
 * self-hosted default organization's page edits the platform's own values,
 * with nothing above them to inherit from, so it badges them the way
 * Platform › Mail relay and the rest badge the very same rows: Override for
 * one stored in the settings table, Env for one the environment delivered.
 */
export function OrgFieldBadge({
  settings,
  section,
  field,
}: {
  settings: OrgSettings
  section: OrgSection
  field: string
}) {
  const source = sourceOf(settings, section, field)
  if (settings.scope === 'operator' && (source === 'override' || source === 'env')) {
    return <SourceBadge source={source} />
  }
  return <OrgSourceBadge source={source} />
}

export type OrgFieldProps = {
  settings: OrgSettings
  draft: OrgDraft
  setField: (field: string, value: DraftValue) => void
}

function formatInherited(value: string | number | boolean): string {
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (typeof value === 'number') return formatNumber(value)
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
  readOnly = false,
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
  /**
   * Shown, never edited here (the platform's embedding endpoint, an env
   * value): rendered as text, not as an input that looks editable.
   */
  readOnly?: boolean
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
      labelRight={<OrgFieldBadge settings={settings} section={section} field={field} />}
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
      htmlFor={readOnly ? false : undefined}
    >
      {readOnly ? (
        <ReadOnlyValue value={String(value)} />
      ) : (
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
      )}
      {error && (
        <p id={errorId} className="mt-1 text-caption text-danger">
          {error}
        </p>
      )}
    </Field>
  )
}
