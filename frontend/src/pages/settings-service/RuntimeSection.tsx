import type { ServiceSettings } from '@/types'
import { Field, SCard, TextInput } from '@/components/settings/kit'
import { FIELD_COPY } from './fieldCopy'
import { NumberSettingInput, OperatorFields, SourceBadge } from './ServiceSettingsPrimitives'
import type { EditableSettings, SectionKey } from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'
import { TelemetryCard } from './TelemetryCard'

export function RuntimeSection({
  form,
  settings,
  setField,
  platformAdmin,
}: {
  form: EditableSettings
  settings: ServiceSettings
  setField: (section: SectionKey, field: string, value: string | number | boolean) => void
  /** The public URL is operator-only (backend `OPERATOR_FIELDS`). */
  platformAdmin: boolean
}) {
  return (
    <>
      <SCard title="Server">
        <OperatorFields locked={!platformAdmin}>
        <Field
          label="App base URL"
          hint="Used in emails, webhooks and the ingest endpoint."
          labelRight={<SourceBadge source={sourceFor(settings, 'runtime', 'app_base_url')} />}
          last
        >
          <TextInput
            value={form.runtime.app_base_url}
            onChange={value => setField('runtime', 'app_base_url', value)}
            placeholder="e.g. https://tripl.example.com"
            mono
          />
        </Field>
        </OperatorFields>
      </SCard>

      {/* The card Organization › Limits shows for the same two values, under
          the same name. */}
      <SCard
        title="Row limits"
        description="Used when a scan or metrics configuration sets no limit of its own. An organization may lower them, never raise them."
      >
        <Field
          label={FIELD_COPY.scan_row_limit_default.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'runtime', 'scan_row_limit_default')} />}
        >
          <NumberSettingInput
            section="runtime"
            field="scan_row_limit_default"
            value={form.runtime.scan_row_limit_default}
            saved={settings.runtime.scan_row_limit_default}
            setField={setField}
            suffix="rows"
          />
        </Field>
        <Field
          label={FIELD_COPY.metrics_row_limit_default.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'runtime', 'metrics_row_limit_default')} />}
          last
        >
          <NumberSettingInput
            section="runtime"
            field="metrics_row_limit_default"
            value={form.runtime.metrics_row_limit_default}
            saved={settings.runtime.metrics_row_limit_default}
            setField={setField}
            suffix="rows"
          />
        </Field>
      </SCard>

      {/* Read from the operator's own route: platform admins only. */}
      {platformAdmin && <TelemetryCard />}
    </>
  )
}
