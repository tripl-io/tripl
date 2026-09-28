import { SCard } from '@/components/settings/kit'
import { OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'

/** Organization › Limits: the row caps a scan or metrics run falls back to. */
export function OrgLimitFields({ settings, draft, setField }: OrgFieldProps) {
  const props = { settings, draft, setField, section: 'limits' as const }
  const ceiling = (value: number) =>
    settings.scope === 'organization'
      ? `Operator maximum: ${value.toLocaleString('en-US')} rows.`
      : undefined
  return (
    <SCard
      title="Row limits"
      description="Used when a scan or metrics configuration sets no limit of its own. An organization may lower them, never raise them above the operator's."
    >
      <OrgTextField
        {...props}
        field="scan_row_limit_default"
        label="Scan row limit default"
        number
        suffix="rows"
        hint={ceiling(settings.ceilings.scan_row_limit_default)}
      />
      <OrgTextField
        {...props}
        field="metrics_row_limit_default"
        label="Metrics row limit default"
        number
        suffix="rows"
        hint={ceiling(settings.ceilings.metrics_row_limit_default)}
        last
      />
    </SCard>
  )
}
