import { useEffect, useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ssoApi, type SsoConfig, type SsoConfigUpdate } from '@/api/sso'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Field, InfoRow, SCard, SHeader, SettingsSaveBar, TextInput, ToggleRow } from '@/components/settings/kit'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgSsoDomainsKey, orgSsoKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { OrgSsoDomainsCard } from './org-settings/OrgSsoDomainsCard'
import {
  ORG_SSO_PATH,
  buildSsoUpdate,
  changedSsoFields,
  enableBlockedReason,
  firstSaveMissing,
  providerConfigured,
  ssoDisplayValue,
  ssoDraftInvalid,
  ssoFieldError,
  ssoRedirectUri,
  type SsoDraft,
  type SsoTextField,
} from './org-settings/orgSsoModel'

const UNSAVED_MESSAGE =
  'Single sign-on settings you edited here have not been saved. Leaving this page drops them.'

/**
 * Organization › Single sign-on (F20): the organization's OpenID Connect
 * provider, the email domains it signs in, and whether it is on and required.
 * Organization OWNERS only; the area shows everyone else a notice.
 */
export default function OrgSsoSection() {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader
        title="Single sign-on"
        description="Let people sign in through your identity provider (OpenID Connect), and optionally require it for everyone in this organization."
      />
      {slug ? (
        <OrgSsoForm key={slug} org={slug} />
      ) : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
    </div>
  )
}

function OrgSsoForm({ org }: { org: string }) {
  const qc = useQueryClient()
  const { registerUnsaved } = useUnsavedChanges()
  const { confirm, dialog } = useConfirm()
  const [draft, setDraft] = useState<SsoDraft>({})
  const [revokedNote, setRevokedNote] = useState<string | null>(null)

  const configQuery = useQuery({
    queryKey: orgSsoKey(org),
    queryFn: () => ssoApi.get(org),
    meta: SILENT_ERROR_META,
  })
  const domainsQuery = useQuery({
    queryKey: orgSsoDomainsKey(org),
    queryFn: () => ssoApi.listDomains(org),
    meta: SILENT_ERROR_META,
  })

  const onSaved = (data: SsoConfig) => {
    qc.setQueryData(orgSsoKey(org), data)
    void qc.invalidateQueries({ queryKey: orgSsoDomainsKey(org) })
  }
  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (update: SsoConfigUpdate) => ssoApi.update(org, update),
    onSuccess: (data) => {
      onSaved(data)
      setDraft({})
    },
  })
  const switchMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (update: SsoConfigUpdate) => ssoApi.update(org, update),
    onSuccess: onSaved,
  })
  const testMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => ssoApi.test(org),
  })

  const config = configQuery.data
  const dirty = config ? changedSsoFields(config, draft).length > 0 : false
  useEffect(() => {
    registerUnsaved(
      dirty ? { keptBy: () => false, message: UNSAVED_MESSAGE, dirtyPaths: [ORG_SSO_PATH] } : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, registerUnsaved])

  const failed = configQuery.isError ? configQuery.error : domainsQuery.isError ? domainsQuery.error : null
  if (failed && !(config && domainsQuery.data)) {
    return (
      <ErrorState
        title="Couldn't load single sign-on settings"
        error={failed}
        onRetry={() => {
          void configQuery.refetch()
          void domainsQuery.refetch()
        }}
      />
    )
  }
  if (!config || !domainsQuery.data) return <SectionSkeleton variant="form" label="Loading single sign-on…" />

  const domains = domainsQuery.data
  const blocked = enableBlockedReason(config, domains)
  const fieldProps = {
    config,
    draft,
    setValue: (field: SsoTextField, value: string) => setDraft((current) => ({ ...current, [field]: value })),
  }

  // A switch saves the whole saved configuration with the one flag changed;
  // unsaved edits to the provider fields stay in the draft.
  const saveSwitches = (switches: { enabled?: boolean; sso_required?: boolean }) =>
    buildSsoUpdate(config, {}, switches)

  const setEnabled = (next: boolean) => {
    setRevokedNote(null)
    if (next) {
      switchMut.mutate(saveSwitches({ enabled: true }))
      return
    }
    void confirm({
      title: 'Turn single sign-on off?',
      message: config.sso_required
        ? 'Nobody can sign in through your identity provider any more, and single sign-on stops being required. People sign in with their passwords; an account first created by single sign-on has none until its owner sets one with Forgot your password.'
        : 'Nobody can sign in through your identity provider any more. People sign in with their passwords; an account first created by single sign-on has none until its owner sets one with Forgot your password.',
      confirmLabel: 'Turn off',
      variant: 'danger',
      errorPrefix: 'Could not turn single sign-on off',
      action: async () =>
        onSaved(await ssoApi.update(org, saveSwitches({ enabled: false, sso_required: false }))),
    })
  }

  const setRequired = (next: boolean) => {
    setRevokedNote(null)
    if (!next) {
      switchMut.mutate(saveSwitches({ sso_required: false }))
      return
    }
    void confirm({
      title: 'Require single sign-on?',
      message: (
        <>
          <p className="m-0">
            Members then use this organization only from a session that signed in through your
            identity provider. Password sessions are refused inside it.
          </p>
          <p className="m-0 mt-2">
            <strong>API keys are revoked</strong> unless they were created from a single sign-on
            session for this organization, owners&rsquo; keys included. New keys need such a
            session too.
          </p>
          <p className="m-0 mt-2">
            Organization owners can still sign in with a password and fix the setup if the identity
            provider fails (break-glass). That covers signing in, not API keys.
          </p>
        </>
      ),
      confirmLabel: 'Require single sign-on',
      variant: 'danger',
      errorPrefix: 'Could not require single sign-on',
      action: async () => {
        const data = await ssoApi.update(org, saveSwitches({ sso_required: true }))
        onSaved(data)
        const revoked = data.revoked_api_keys ?? 0
        setRevokedNote(
          revoked > 0
            ? `Single sign-on is now required. ${revoked} API key${revoked === 1 ? ' was' : 's were'} revoked.`
            : 'Single sign-on is now required. No API keys needed revoking.',
        )
      },
    })
  }

  return (
    <div className="min-w-0 space-y-5">
      <SettingsSaveBar
        note="The provider settings apply from the next sign-in."
        error={saveMut.isError ? getErrorMessage(saveMut.error) : undefined}
        dirty={dirty}
        invalid={ssoDraftInvalid(draft) || firstSaveMissing(config, draft)}
        invalidMessage={
          firstSaveMissing(config, draft)
            ? 'Enter the issuer URL and client ID to save.'
            : undefined
        }
        pending={saveMut.isPending}
        onDiscard={() => setDraft({})}
        onSave={() => saveMut.mutate(buildSsoUpdate(config, draft))}
      />

      <SCard
        title="Identity provider"
        description="Register tripl as a web application (authorization code flow) at your OpenID Connect provider, then copy its issuer URL, client ID and client secret here."
      >
        <InfoRow label="Redirect URI" value={ssoRedirectUri(window.location.origin, org, config.redirect_uri)} />
        <SsoTextInput
          {...fieldProps}
          field="issuer"
          label="Issuer URL"
          placeholder="e.g. https://idp.example.com"
          hint="https only, and it must be a public address. tripl reads its discovery document at /.well-known/openid-configuration."
        />
        <SsoTextInput {...fieldProps} field="client_id" label="Client ID" placeholder="e.g. tripl" />
        <SsoTextInput
          {...fieldProps}
          field="client_secret"
          label="Client secret"
          secret
          placeholder={config.client_secret_configured ? 'Configured — leave blank to keep' : 'Not configured'}
          hint={config.client_secret_configured ? 'Stored encrypted and never shown again; type a new one to replace it.' : undefined}
        />
        <SsoTextInput
          {...fieldProps}
          field="scopes"
          label="Scopes"
          hint="Space-separated. openid and email are needed; the provider must also send email_verified."
          last
        />
      </SCard>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="outline"
          disabled={testMut.isPending || dirty || !config.issuer}
          onClick={() => testMut.mutate()}
        >
          {testMut.isPending ? 'Testing…' : 'Test connection'}
        </Button>
        {dirty && <span className="text-body-sm text-fg-tertiary">Save first to test the saved settings.</span>}
        {testMut.isSuccess && (
          <span role="status" className={testMut.data.ok ? 'text-body-sm text-fg-muted' : 'text-body-sm text-danger'}>
            {testMut.data.message}
          </span>
        )}
        {testMut.isError && (
          <span role="alert" className="text-body-sm text-danger">
            {getErrorMessage(testMut.error)}
          </span>
        )}
      </div>

      <OrgSsoDomainsCard org={org} domains={domains} enabled={config.enabled} />

      <SCard title="Sign-in" description="Turn single sign-on on once the provider is saved and a domain is verified.">
        <ToggleRow
          label="Enable single sign-on"
          hint={
            config.enabled
              ? 'People at the verified domains can sign in with SSO. New addresses get an account and join as members; an existing account is linked only after its owner confirms.'
              : blocked ?? 'People at the verified domains can then sign in with SSO.'
          }
          value={config.enabled}
          disabled={switchMut.isPending || (!config.enabled && blocked !== null)}
          onChange={setEnabled}
        />
        <ToggleRow
          label="Require single sign-on"
          hint={
            config.enabled
              ? "Password sessions and API keys not created from a single sign-on session are refused in this organization. Owners can still sign in with a password (break-glass), but their keys follow the rule too."
              : 'Turn single sign-on on first.'
          }
          value={config.sso_required}
          disabled={switchMut.isPending || (!config.sso_required && (!config.enabled || !providerConfigured(config)))}
          onChange={setRequired}
          last
        />
        {switchMut.isError && (
          <p role="alert" className="m-0 px-4 pb-3 text-body-sm text-danger">
            {getErrorMessage(switchMut.error)}
          </p>
        )}
        {revokedNote && (
          <p role="status" className="m-0 px-4 pb-3 text-body-sm text-fg-muted">
            {revokedNote}
          </p>
        )}
      </SCard>
      {dialog}
    </div>
  )
}

function SsoTextInput({
  config,
  draft,
  setValue,
  field,
  label,
  placeholder,
  hint,
  secret,
  last,
}: {
  config: SsoConfig
  draft: SsoDraft
  setValue: (field: SsoTextField, value: string) => void
  field: SsoTextField
  label: string
  placeholder?: string
  hint?: string
  secret?: boolean
  last?: boolean
}) {
  const errorId = useId()
  const error = ssoFieldError(field, draft)
  return (
    <Field label={label} hint={hint} last={last}>
      <TextInput
        type={secret ? 'password' : 'text'}
        autoComplete="off"
        value={ssoDisplayValue(config, draft, field)}
        onChange={(next) => setValue(field, next)}
        placeholder={placeholder}
        mono={!secret}
        aria-invalid={error !== null}
        aria-describedby={error ? errorId : undefined}
      />
      {error && (
        <p id={errorId} className="mt-1 text-caption text-danger">
          {error}
        </p>
      )}
    </Field>
  )
}
