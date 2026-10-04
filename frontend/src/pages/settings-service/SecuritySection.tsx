import type { ServiceSettings } from '@/types'
import { Field, SCard, NativeSelect, TextArea, TextInput, ToggleRow } from '@/components/settings/kit'
import { InactiveGroup, NumberSettingInput, SourceBadge } from './ServiceSettingsPrimitives'
import type { EditableSettings, SectionKey } from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'

// Short enough to fit the kit's 280px Select. The full sentence — "Open —
// anyone who can reach this instance can sign up" — clipped mid-word against
// the chevron ("...can reach this insta") with no ellipsis, because a native
// <select> with appearance-none hard-clips its own value. The
// setting that decides who may create an account on this server was the one
// value on the page you could not read. What "Open" exposes is spelled out in
// the field hint below, which has the width for it.
const REGISTRATION_OPTIONS = [
  { value: 'open', label: 'Open — anyone can sign up' },
  { value: 'disabled', label: 'Disabled — no new accounts' },
] as const

export function SecuritySection({
  form,
  settings,
  setField,
}: {
  form: EditableSettings
  settings: ServiceSettings
  setField: (section: SectionKey, field: string, value: string | number | boolean) => void
}) {
  const registrationOpen = form.security.registration_mode === 'open'

  return (
    <>
      {/* The "applies on the very next signup attempt" exception is stated once,
          in the page's save-row note (applyNote('security')), which is two lines
          above this card. It used to be repeated here in a second vocabulary —
          "no redeploy" against the note's "no restart" — so the same caveat
          appeared twice within 200px in two different words. */}
      <SCard
        title="Registration"
        description="Who is allowed to create an account on this instance."
      >
        <Field
          label="Self-service registration"
          htmlFor="security-registration-mode"
          labelRight={<SourceBadge source={sourceFor(settings, 'security', 'registration_mode')} />}
          // Stacked: this hint is the longest on the page and the side-by-side
          // layout crams it into the 232px label gutter — nine lines, wrapping
          // "owner-only" across two of them, with ~380px of the row empty beside
          // it. The most consequential explanation on the page was the hardest
          // thing to read on it.
          stacked
          hint={
            registrationOpen
              ? 'Open: anyone who can reach this instance can create an account. A new account joins as editor — it can read the whole tracking plan and the member roster, and edit any shared project. Data source connection details stay owner-only. Close this once your team has accounts.'
              : 'Disabled: POST /auth/register is refused with a 403 — except the very first account on an instance with no users, which always works and becomes owner. You can still add people while this is off: Settings → Organization → Invitations issues a single-use link for one address, at a role you pick.'
          }
          last
        >
          <NativeSelect
            id="security-registration-mode"
            value={form.security.registration_mode}
            onChange={value => setField('security', 'registration_mode', value)}
            options={REGISTRATION_OPTIONS}
            width="fill"
          />
        </Field>
      </SCard>

      <SCard title="Sessions">
        <Field
          label="Session cookie"
          labelRight={<SourceBadge source={sourceFor(settings, 'security', 'session_cookie_name')} />}
          hint="Renaming it signs everyone out after the next restart."
        >
          <TextInput
            value={form.security.session_cookie_name}
            onChange={value => setField('security', 'session_cookie_name', value)}
            mono
          />
        </Field>
        <Field
          label="Session TTL"
          labelRight={<SourceBadge source={sourceFor(settings, 'security', 'session_ttl_hours')} />}
        >
          <NumberSettingInput
            section="security"
            field="session_ttl_hours"
            value={form.security.session_ttl_hours}
            saved={settings.security.session_ttl_hours}
            setField={setField}
            suffix="hours"
          />
        </Field>
        <ToggleRow
          label="Secure cookie"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'security', 'session_cookie_secure')} />
          }
          hint="Sent over HTTPS only. On an instance served over plain HTTP, nobody can sign in after the next restart."
          value={form.security.session_cookie_secure}
          onChange={value => setField('security', 'session_cookie_secure', value)}
          last
        />
      </SCard>

      <SCard title="Network & headers">
        <Field
          label="CORS allow origins"
          labelRight={<SourceBadge source={sourceFor(settings, 'security', 'cors_allow_origins')} />}
          hint="Browser clients on an origin missing here are refused after the next restart."
        >
          <TextInput
            value={form.security.cors_allow_origins}
            onChange={value => setField('security', 'cors_allow_origins', value)}
            placeholder="e.g. https://app.example.com,https://admin.example.com"
            mono
          />
        </Field>
        <ToggleRow
          label="Security headers"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'security', 'security_headers_enabled')} />
          }
          value={form.security.security_headers_enabled}
          onChange={value => setField('security', 'security_headers_enabled', value)}
        />
        <ToggleRow
          label="HSTS"
          labelRight={<SourceBadge source={sourceFor(settings, 'security', 'hsts_enabled')} />}
          value={form.security.hsts_enabled}
          onChange={value => setField('security', 'hsts_enabled', value)}
        />
        {/* Editable, but visibly idle while its switch is off. */}
        <InactiveGroup inactive={!form.security.hsts_enabled} reason="Not used while HSTS is off.">
          <Field
            label="HSTS max age"
            labelRight={<SourceBadge source={sourceFor(settings, 'security', 'hsts_max_age_seconds')} />}
          >
            <NumberSettingInput
              section="security"
              field="hsts_max_age_seconds"
              value={form.security.hsts_max_age_seconds}
              saved={settings.security.hsts_max_age_seconds}
              setField={setField}
              suffix="seconds"
            />
          </Field>
        </InactiveGroup>
        <Field
          label="Content Security Policy"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'security', 'content_security_policy')} />
          }
          hint="A policy that blocks this app's own scripts or API calls stops the app from loading after the next restart."
          stacked
          last
        >
          <TextArea
            value={form.security.content_security_policy}
            onChange={value => setField('security', 'content_security_policy', value)}
          />
        </Field>
      </SCard>

      <SCard title="Rate limiting">
        <ToggleRow
          label="Rate limiting"
          labelRight={<SourceBadge source={sourceFor(settings, 'security', 'rate_limit_enabled')} />}
          value={form.security.rate_limit_enabled}
          onChange={value => setField('security', 'rate_limit_enabled', value)}
        />
        <InactiveGroup
          inactive={!form.security.rate_limit_enabled}
          reason="Not used while Rate limiting is off."
        >
        <Field
          label="Login limit"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'security', 'rate_limit_login_per_minute')} />
          }
        >
          <NumberSettingInput
            section="security"
            field="rate_limit_login_per_minute"
            value={form.security.rate_limit_login_per_minute}
            saved={settings.security.rate_limit_login_per_minute}
            setField={setField}
            suffix="/min"
          />
        </Field>
        <Field
          label="Register limit"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'security', 'rate_limit_register_per_hour')} />
          }
        >
          <NumberSettingInput
            section="security"
            field="rate_limit_register_per_hour"
            value={form.security.rate_limit_register_per_hour}
            saved={settings.security.rate_limit_register_per_hour}
            setField={setField}
            suffix="/hour"
          />
        </Field>
        <ToggleRow
          label="Trust X-Forwarded-For"
          labelRight={
            <SourceBadge
              source={sourceFor(settings, 'security', 'rate_limit_trust_forwarded_for')}
            />
          }
          value={form.security.rate_limit_trust_forwarded_for}
          onChange={value => setField('security', 'rate_limit_trust_forwarded_for', value)}
          last
        />
        </InactiveGroup>
      </SCard>
    </>
  )
}
