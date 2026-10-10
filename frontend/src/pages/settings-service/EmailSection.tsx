import { MailCheck } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { serviceSettingsApi } from '@/api/serviceSettings'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import type { ServiceSettings } from '@/types'
import { Button } from '@/components/ui/button'
import { Field, SCard, NativeSelect, TextInput } from '@/components/settings/kit'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { FIELD_COPY, SMTP_CARD_TITLE, SMTP_SECURITY_HINTS, SMTP_SECURITY_OPTIONS } from './fieldCopy'
import { NumberSettingInput, SourceBadge, StatusBadge } from './ServiceSettingsPrimitives'
import type {
  EditableSettings,
  SecretDrafts,
  SecretField,
  SectionKey,
} from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'

/**
 * Platform › Mail relay. The fields come in the order Organization › Email
 * shows the same values in (host, port, security, sign-in, sender), on one
 * card: on a self-hosted instance the default organization's Email page edits
 * this very relay.
 */
export function EmailSection({
  form,
  settings,
  secretDrafts,
  setField,
  setSecretDrafts,
  saving,
  onClearSecret,
}: {
  form: EditableSettings
  settings: ServiceSettings
  secretDrafts: SecretDrafts
  setField: (section: SectionKey, field: string, value: string | number | boolean) => void
  setSecretDrafts: (updater: (current: SecretDrafts) => SecretDrafts) => void
  saving: boolean
  onClearSecret: (section: 'ai' | 'email', field: SecretField) => void
}) {
  const emailTestMut = useMutation({
    mutationFn: () => serviceSettingsApi.testEmail(),
    // A failed request is rendered in the status slot beside the button.
    meta: SILENT_ERROR_META,
  })
  // The probe sends with the SAVED settings and needs both a host and a From:
  // address (the backend's `email_can_send`); without either it can only
  // fail, so it waits for them.
  const testBlocker = !settings.email.smtp_host.trim()
    ? 'Set an SMTP host and save first.'
    : !settings.email.smtp_from_address.trim()
      ? 'Set a default From address and save first.'
      : null

  return (
    <>
      <SCard title={SMTP_CARD_TITLE}>
        <Field
          label={FIELD_COPY.smtp_host.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'email', 'smtp_host')} />}
        >
          <TextInput
            value={form.email.smtp_host}
            onChange={value => setField('email', 'smtp_host', value)}
            placeholder={examplePlaceholder('smtp.example.com')}
            mono
          />
        </Field>
        <Field
          label={FIELD_COPY.smtp_port.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'email', 'smtp_port')} />}
        >
          <NumberSettingInput
            section="email"
            field="smtp_port"
            value={form.email.smtp_port}
            saved={settings.email.smtp_port}
            setField={setField}
          />
        </Field>
        <Field
          label={FIELD_COPY.smtp_security.label}
          htmlFor="email-smtp-security"
          labelRight={<SourceBadge source={sourceFor(settings, 'email', 'smtp_security')} />}
          // Stacked because the hint is a sentence, and this is the one field on
          // the card whose wrong value produces no error message anywhere — the
          // send just hangs. It replaced a "Use TLS" switch that could only ever
          // mean STARTTLS, which is why a 465 relay was unreachable however it
          // was set.
          stacked
          hint={SMTP_SECURITY_HINTS[form.email.smtp_security]}
        >
          <NativeSelect
            id="email-smtp-security"
            value={form.email.smtp_security}
            onChange={value => setField('email', 'smtp_security', value)}
            options={SMTP_SECURITY_OPTIONS}
            width="fill"
          />
        </Field>
        <Field
          label={FIELD_COPY.smtp_username.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'email', 'smtp_username')} />}
        >
          <TextInput
            value={form.email.smtp_username}
            onChange={value => setField('email', 'smtp_username', value)}
            placeholder={examplePlaceholder('tripl@example.com')}
            mono
          />
        </Field>
        <Field
          label={FIELD_COPY.smtp_password.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'email', 'smtp_password')} />}
        >
          <div className="flex gap-2">
            <div className="flex-1">
              <TextInput
                type="password"
                value={secretDrafts.smtp_password}
                onChange={value =>
                  setSecretDrafts(current => ({ ...current, smtp_password: value }))
                }
                placeholder={
                  form.email.smtp_password_configured
                    ? 'Configured — leave blank to keep'
                    : 'Not configured'
                }
              />
            </div>
            {/* Only with a password stored: with none there is nothing to
                delete, and a disabled button only said so in grey. */}
            {form.email.smtp_password_configured && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => onClearSecret('email', 'smtp_password')}
                disabled={saving}
              >
                Delete stored password
              </Button>
            )}
          </div>
        </Field>
        <Field
          label={FIELD_COPY.smtp_from_address.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'email', 'smtp_from_address')} />}
          last
        >
          <TextInput
            value={form.email.smtp_from_address}
            onChange={value => setField('email', 'smtp_from_address', value)}
            placeholder={examplePlaceholder('alerts@example.com')}
            mono
          />
        </Field>
      </SCard>

      {/* Named for what it does; "Check" told the reader little. */}
      <SCard
        title="Send a test email"
        description="Sends one message to your own address. Uses the saved settings, so save your changes first."
      >
        {/* A test button and its status line, not a control to be named —
            the same shape the AI section uses. This card is the whole point of
            a failed password-reset send is deliberately invisible to
            the person who asked for the link, so the operator needs somewhere
            else to look, and until now there was nowhere. */}
        <Field label="Delivery" last htmlFor={false}>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => emailTestMut.mutate()}
              disabled={emailTestMut.isPending || saving || testBlocker !== null}
              {...disabledReasonAria('email-test', testBlocker)}
            >
              <MailCheck className="h-3.5 w-3.5" />
              {emailTestMut.isPending ? 'Sending...' : 'Send test email'}
            </Button>
            <span role="status" aria-live="polite" aria-atomic="true" className="inline-flex">
              {emailTestMut.isError ? (
                <StatusBadge active={false} label={getErrorMessage(emailTestMut.error)} />
              ) : (
                emailTestMut.data && (
                  <StatusBadge active={emailTestMut.data.ok} label={emailTestMut.data.message} />
                )
              )}
            </span>
          </div>
          <DisabledReason id="email-test" reason={testBlocker} className="mt-1.5" />
        </Field>
      </SCard>
    </>
  )
}
