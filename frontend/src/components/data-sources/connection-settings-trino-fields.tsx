import type { TrinoHttpScheme } from '@/types'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FieldError } from '@/components/forms/FieldError'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { invalidAria } from '@/components/forms/validation'
import {
  FIELD_COL_CLASS,
  HELP_CLASS,
  MAX_TRINO_SCHEMA_ALLOWLIST,
  SELECT_CLASS,
  TRINO_SCHEME_OPTIONS,
  type ConnectionSettingsForm,
} from './connection-settings'

interface TrinoFieldsProps {
  idPrefix: string
  value: ConnectionSettingsForm
  onChange: (patch: Partial<ConnectionSettingsForm>) => void
}

/** Trino / Starburst: the coordinator's scheme, the default schema, what to browse. */
export function TrinoSettingsFields({ idPrefix, value, onChange }: TrinoFieldsProps) {
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
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-trino-schema-name`}>Default schema</Label>
          <Input
            id={`${idPrefix}-trino-schema-name`}
            value={value.schemaName}
            onChange={(e) => onChange({ schemaName: e.target.value })}
            placeholder={examplePlaceholder('events')}
          />
          <p className={HELP_CLASS}>
            Where unqualified table names resolve. Empty means every name is schema-qualified.
          </p>
        </div>
      </div>
      <div className={FIELD_COL_CLASS}>
        <Label htmlFor={`${idPrefix}-trino-schema-allowlist`}>Schema allowlist</Label>
        <Input
          id={`${idPrefix}-trino-schema-allowlist`}
          value={value.schemaAllowlist}
          onChange={(e) => onChange({ schemaAllowlist: e.target.value })}
          placeholder={examplePlaceholder('events, marts')}
        />
        <p className={HELP_CLASS}>
          Comma-separated schemas of the catalog the schema browser may list. Empty with no
          default schema lists the whole catalog. At most {MAX_TRINO_SCHEMA_ALLOWLIST}.
        </p>
      </div>
    </>
  )
}

interface AthenaFieldsProps extends TrinoFieldsProps {
  s3OutputError?: string
}

/** Amazon Athena: the workgroup, where results are written, the catalog, what to browse. */
export function AthenaSettingsFields({
  idPrefix,
  value,
  onChange,
  s3OutputError,
}: AthenaFieldsProps) {
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
            {...invalidAria(outputId, s3OutputError)}
          />
          <FieldError inputId={outputId} message={s3OutputError} />
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
        <div className={FIELD_COL_CLASS}>
          <Label htmlFor={`${idPrefix}-athena-schema-allowlist`}>Database allowlist</Label>
          <Input
            id={`${idPrefix}-athena-schema-allowlist`}
            value={value.schemaAllowlist}
            onChange={(e) => onChange({ schemaAllowlist: e.target.value })}
            placeholder={examplePlaceholder('events, marts')}
          />
          <p className={HELP_CLASS}>
            Further Glue databases the schema browser may list. At most{' '}
            {MAX_TRINO_SCHEMA_ALLOWLIST}.
          </p>
        </div>
      </div>
    </>
  )
}
