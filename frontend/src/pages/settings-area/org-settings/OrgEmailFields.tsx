import { MailCheck } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { orgSettingsApi } from '@/api/orgSettings'
import { Button } from '@/components/ui/button'
import { Field, NativeSelect, SCard, TextInput } from '@/components/settings/kit'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import {
  FIELD_COPY,
  SMTP_CARD_TITLE,
  SMTP_SECURITY_HINTS,
  SMTP_SECURITY_OPTIONS,
} from '@/pages/settings-service/fieldCopy'
import { StatusBadge } from '@/pages/settings-service/ServiceSettingsPrimitives'
import {
  SMTP_GROUP,
  clearGroup,
  displayValue,
  groupOwned,
  groupWarning,
  type OrgDraft,
} from './orgSettingsModel'
import { InheritHint, OrgFieldBadge, OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'

/**
 * Organization › Email: the relay THIS organization's alerts, digests and
 * notifications go out through. Account mail (sign-up, password reset,
 * invitations) always uses the platform's relay, whatever is set here. The
 * labels, choices and field order are Platform › Mail relay's (fieldCopy.ts):
 * on a self-hosted instance the default organization edits that very relay
 * here.
 */
export function OrgEmailFields({
  org,
  settings,
  draft,
  setField,
  setDraft,
  saving,
}: OrgFieldProps & {
  org: string
  setDraft: (next: OrgDraft) => void
  saving: boolean
}) {
  const section = 'email' as const
  const props = { settings, draft, setField, section }
  const warning = groupWarning(settings, draft, section)
  const owned = settings.scope === 'organization' && groupOwned(settings, section, SMTP_GROUP)
  const passwordConfigured = settings.email.smtp_password_configured
  const security = String(displayValue(settings, draft, section, 'smtp_security'))
  const testMut = useMutation({
    mutationFn: () => orgSettingsApi.testEmail(org),
    meta: SILENT_ERROR_META,
  })
  const testBlocker = !settings.email.smtp_host.trim()
    ? 'No SMTP host is in effect for this organization. Set one and save first.'
    : !settings.email.smtp_from_address.trim()
      ? 'No From address is in effect for this organization. Set one and save first.'
      : null

  return (
    <>
      <SCard
        title={SMTP_CARD_TITLE}
        // The group rule is about inheriting; the platform's own relay (the
        // self-hosted default organization's page) inherits from nothing.
        description={
          settings.scope === 'organization'
            ? "Host, port, security, sign-in and sender are one group: once this organization sets any of them it stops inheriting the rest, and the platform's password is never sent to its relay."
            : undefined
        }
        footer={
          owned ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setDraft(clearGroup(draft, settings, section, SMTP_GROUP))}
              disabled={saving}
            >
              Use the platform&rsquo;s relay
            </Button>
          ) : undefined
        }
      >
        {warning && (
          <p role="note" className="m-0 px-4 pt-2.5 text-caption text-(--warning)">
            {warning.starting &&
              'Saving makes the whole relay this organization’s own: fields you leave as they are take the built-in defaults, not the platform’s values. '}
            {warning.missingSecret &&
              'No password of this organization’s: leave it empty only if the relay needs none.'}
          </p>
        )}
        <OrgTextField
          {...props}
          field="smtp_host"
          label={FIELD_COPY.smtp_host.label}
          grouped
          placeholder={examplePlaceholder('smtp.example.com')}
        />
        <OrgTextField {...props} field="smtp_port" label={FIELD_COPY.smtp_port.label} grouped number />
        <Field
          label={FIELD_COPY.smtp_security.label}
          htmlFor="org-smtp-security"
          labelRight={<OrgFieldBadge settings={settings} section={section} field="smtp_security" />}
          // Stacked with what the mode does, as on Platform › Mail relay: a
          // mode that disagrees with the port gives no error, the send hangs.
          stacked
          hint={SMTP_SECURITY_HINTS[security]}
        >
          <NativeSelect
            id="org-smtp-security"
            value={security}
            onChange={value => setField('smtp_security', value)}
            options={SMTP_SECURITY_OPTIONS}
            width="fill"
          />
        </Field>
        <OrgTextField
          {...props}
          field="smtp_username"
          label={FIELD_COPY.smtp_username.label}
          grouped
          placeholder={examplePlaceholder('tripl@example.com')}
        />
        <Field
          label={FIELD_COPY.smtp_password.label}
          labelRight={<OrgFieldBadge settings={settings} section={section} field="smtp_password" />}
          hint={<InheritHint {...props} field="smtp_password" grouped />}
        >
          <TextInput
            type="password"
            value={String(displayValue(settings, draft, section, 'smtp_password'))}
            onChange={value => setField('smtp_password', value)}
            placeholder={passwordConfigured ? 'Configured — leave blank to keep' : 'Not configured'}
          />
        </Field>
        <OrgTextField
          {...props}
          field="smtp_from_address"
          label={FIELD_COPY.smtp_from_address.label}
          grouped
          placeholder={examplePlaceholder('alerts@example.com')}
          last
        />
      </SCard>

      <SCard
        title="Send a test email"
        description="Sends one message to your own address through the relay this organization uses. Uses the saved settings, so save your changes first."
      >
        <Field label="Delivery" last htmlFor={false}>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => testMut.mutate()}
              disabled={testMut.isPending || saving || testBlocker !== null}
              {...disabledReasonAria('org-email-test', testBlocker)}
            >
              <MailCheck className="h-3.5 w-3.5" />
              {testMut.isPending ? 'Sending...' : 'Send test email'}
            </Button>
            <span role="status" aria-live="polite" aria-atomic="true" className="inline-flex">
              {testMut.isError ? (
                <StatusBadge active={false} label={getErrorMessage(testMut.error)} />
              ) : (
                testMut.data && <StatusBadge active={testMut.data.ok} label={testMut.data.message} />
              )}
            </span>
          </div>
          <DisabledReason id="org-email-test" reason={testBlocker} className="mt-1.5" />
        </Field>
      </SCard>
    </>
  )
}
