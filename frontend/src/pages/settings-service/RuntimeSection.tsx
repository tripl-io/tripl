import type { ServiceSettings } from '@/types'
import { Field, SCard, TextInput } from '@/components/settings/kit'
import { NumberSettingInput, OperatorFields, SourceBadge } from './ServiceSettingsPrimitives'
import type { EditableSettings, SectionKey } from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'

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

      <SCard title="Query limits">
        <Field
          label="Scan row limit default"
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
          label="Metrics row limit default"
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
    </>
  )
}
