import { SCard } from '@/components/settings/kit'
import { FIELD_COPY } from '@/pages/settings-service/fieldCopy'
import { OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'
import { formatNumber } from '@/lib/format'

/**
 * Organization › Limits: the row caps a scan or metrics run falls back to. The
 * card, labels and units are Platform › Runtime's "Row limits", which holds the
 * platform's own values (the self-hosted default organization's, here).
 */
export function OrgLimitFields({ settings, draft, setField }: OrgFieldProps) {
  const props = { settings, draft, setField, section: 'limits' as const }
  const organizationScope = settings.scope === 'organization'
  const ceiling = (value: number) =>
    organizationScope ? `Platform maximum: ${formatNumber(value)} rows.` : undefined
  return (
    <SCard
      title="Row limits"
      description={
        organizationScope
          ? "Used when a scan or metrics configuration sets no limit of its own. This organization may lower them, never raise them above the platform's."
          : 'Used when a scan or metrics configuration sets no limit of its own. Other organizations may lower them, never raise them.'
      }
    >
      <OrgTextField
        {...props}
        field="scan_row_limit_default"
        label={FIELD_COPY.scan_row_limit_default.label}
        number
        suffix={FIELD_COPY.scan_row_limit_default.suffix}
        hint={ceiling(settings.ceilings.scan_row_limit_default)}
      />
      <OrgTextField
        {...props}
        field="metrics_row_limit_default"
        label={FIELD_COPY.metrics_row_limit_default.label}
        number
        suffix={FIELD_COPY.metrics_row_limit_default.suffix}
        hint={ceiling(settings.ceilings.metrics_row_limit_default)}
        last
      />
    </SCard>
  )
}
