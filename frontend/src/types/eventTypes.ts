export interface EventType {
  id: string
  project_id: string
  name: string
  display_name: string
  description: string
  color: string
  order: number
  created_at: string
  updated_at: string
  field_definitions: FieldDefinition[]
  /** The scan naming rule that governs this type, resolved server-side — for a
   * branch copy, through its main counterpart. Null when no scan names the
   * type and a free-text name is the identity. Absent from an older instance. */
  event_name_format?: string | null
}

export interface EventTypeBrief {
  id: string
  name: string
  display_name: string
  color: string
}

export interface FieldDefinition {
  id: string
  event_type_id: string
  name: string
  display_name: string
  field_type: 'string' | 'number' | 'boolean' | 'json' | 'enum' | 'url'
  is_required: boolean
  enum_options: string[] | null
  description: string
  order: number
  sensitivity: Sensitivity
  contract_required_max_null_rate?: number | null
  contract_regex?: string | null
  contract_min_value?: number | null
  contract_max_value?: number | null
  contract_max_bad_rate?: number
}

export type Sensitivity = 'none' | 'pii' | 'phi' | 'financial' | 'secret'

export const SENSITIVITY_OPTIONS: {
  value: Sensitivity
  label: string
}[] = [
  { value: 'none', label: 'None' },
  { value: 'pii', label: 'PII' },
  { value: 'phi', label: 'PHI' },
  { value: 'financial', label: 'Financial' },
  { value: 'secret', label: 'Secret' },
]

export type SensitivityStyle = { bg: string; fg: string }

/**
 * Canonical sensitivity chip colors — matches the design mockup `SENS_STYLE`
 * (`design/tripl/project/surface-pages.jsx`). Single source of truth for the
 * sensitivity pill rendered by `SensitivityChip`, shared by the events/types
 * surface tasks. `none` renders as an em-dash, so it has no entry here.
 */
export const SENSITIVITY_STYLE: Record<Exclude<Sensitivity, 'none'>, SensitivityStyle> = {
  pii: { bg: 'oklch(0.7 0.17 15 / 0.16)', fg: 'oklch(0.8 0.16 15)' },
  phi: { bg: 'oklch(0.7 0.16 300 / 0.16)', fg: 'oklch(0.8 0.15 300)' },
  financial: { bg: 'oklch(0.78 0.15 75 / 0.16)', fg: 'oklch(0.84 0.13 75)' },
  secret: { bg: 'oklch(0.42 0.02 250)', fg: 'oklch(0.92 0.01 250)' },
}

export interface EventTypeRelation {
  id: string
  project_id: string
  source_event_type_id: string
  target_event_type_id: string
  source_field_id: string
  target_field_id: string
  relation_type: string
  description: string
}

/** GET /meta-fields/{id}/usage: what deleting the field would clear. */
export interface MetaFieldUsage {
  // Non-empty stored values; a multi-value field holds several per event.
  value_count: number
  // Events holding at least one of them.
  event_count: number
}

export interface MetaFieldDefinition {
  id: string
  project_id: string
  name: string
  display_name: string
  field_type: 'string' | 'url' | 'boolean' | 'enum' | 'date'
  is_required: boolean
  /**
   * The event may carry several values for this field, the way it carries
   * several tags. Optional to mirror the backend default: an older response
   * omits it, and a missing flag must read as single-valued.
   */
  allow_multiple?: boolean
  enum_options: string[] | null
  default_value: string | null
  link_template: string | null
  order: number
  sensitivity: Sensitivity
}

/**
 * The swatch an event type, metric or fact table starts with when nobody has
 * picked one. It has to equal the backend column default (the API schemas
 * document `@default #6366f1`), so a row created without a colour and a form
 * opened fresh show the same swatch; it was repeated as a magic hex in five
 * places.
 */
export const DEFAULT_ENTITY_COLOR = '#6366f1'
