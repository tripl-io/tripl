import type { TrinoHttpScheme } from '@/types'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FieldError } from '@/components/forms/FieldError'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { invalidAria } from '@/components/forms/validation'
import {
  FIELD_COL_CLASS,
  HELP_CLASS,
  MAX_SCHEMA_ALLOWLIST,
  SELECT_CLASS,
  TRINO_SCHEME_OPTIONS,
  type ConnectionSettingsForm,
  type SettingsErrors,
} from './connection-settings'
import { ScopeField } from './scope-field'

interface TrinoFieldsProps {
  idPrefix: string
  value: ConnectionSettingsForm
  onChange: (patch: Partial<ConnectionSettingsForm>) => void
  errors: SettingsErrors
}

/** Trino / Starburst: the coordinator's scheme, the default schema, what to browse. */
export function TrinoSettingsFields({ idPrefix, value, onChange, errors }: TrinoFieldsProps) {
  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-trino-scheme`}>Scheme</Label>
          <select
            id={`${idPrefix}-trino-scheme`}
            value={value.httpScheme}
            onChange={(e) => onChange({ httpScheme: e.target.value as TrinoHttpScheme })}
            className={SELECT_CLASS}
          >
            {TRINO_SCHEME_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <p className={HELP_CLASS}>
            A password is only ever sent over HTTPS. Leave it empty for a coordinator without
            authentication.
          </p>
        </div>
        <ScopeField
          id={`${idPrefix}-schema-name`}
          label="Default schema"
          value={value.schemaName}
          onChange={(schemaName) => onChange({ schemaName })}
          placeholder={examplePlaceholder('events')}
          help="Where unqualified table names resolve. Empty means every name is schema-qualified."
          error={errors.schemaName}
        />
      </div>
      <ScopeField
        id={`${idPrefix}-schema-allowlist`}
        label="Schema allowlist"
        value={value.schemaAllowlist}
        onChange={(schemaAllowlist) => onChange({ schemaAllowlist })}
        placeholder={examplePlaceholder('events, marts')}
        help={`Comma-separated schemas of the catalog the schema browser may list. Empty with no default schema lists the whole catalog. At most ${MAX_SCHEMA_ALLOWLIST}.`}
        error={errors.schemaAllowlist}
      />
    </>
  )
}

/** Amazon Athena: the workgroup, where results are written, the catalog, what to browse. */
export function AthenaSettingsFields({ idPrefix, value, onChange, errors }: TrinoFieldsProps) {
  const outputId = `${idPrefix}-athena-output`
  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-athena-workgroup`}>Workgroup</Label>
          <Input
            id={`${idPrefix}-athena-workgroup`}
            value={value.workGroup}
            onChange={(e) => onChange({ workGroup: e.target.value })}
            placeholder={examplePlaceholder('primary')}
          />
          <p className={HELP_CLASS}>
            Where queries run and are billed. Empty means primary. Its data-scanned limit
            applies to every tripl query.
          </p>
        </div>
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={outputId}>Query result location</Label>
          <Input
            id={outputId}
            value={value.s3OutputLocation}
            onChange={(e) => onChange({ s3OutputLocation: e.target.value })}
            placeholder={examplePlaceholder('s3://my-bucket/athena-results/')}
            {...invalidAria(outputId, errors.s3OutputLocation)}
          />
          <FieldError inputId={outputId} message={errors.s3OutputLocation} />
          <p className={HELP_CLASS}>
            Where Athena writes results. Empty uses the workgroup’s own location.
          </p>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-athena-catalog`}>Data catalog</Label>
          <Input
            id={`${idPrefix}-athena-catalog`}
            value={value.catalogName}
            onChange={(e) => onChange({ catalogName: e.target.value })}
            placeholder={examplePlaceholder('AwsDataCatalog')}
          />
          <p className={HELP_CLASS}>Empty means AwsDataCatalog, the Glue catalog.</p>
        </div>
        <ScopeField
          id={`${idPrefix}-schema-allowlist`}
          label="Database allowlist"
          value={value.schemaAllowlist}
          onChange={(schemaAllowlist) => onChange({ schemaAllowlist })}
          placeholder={examplePlaceholder('events, marts')}
          help={`Further Glue databases the schema browser may list. At most ${MAX_SCHEMA_ALLOWLIST}.`}
          error={errors.schemaAllowlist}
        />
      </div>
    </>
  )
}
