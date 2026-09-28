import { useId, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ssoApi, type SamlCertInfo, type SamlMetadataImport, type SsoConfig } from '@/api/sso'
import { Field, InfoRow, NativeSelect, SCard, TextArea } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { formatDate } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import {
  SAML_NAME_ID_FORMATS,
  certExpiryState,
  formatFingerprint,
  samlSpValues,
  ssoDisplayValue,
  type SsoDraft,
  type SsoTextField,
} from './orgSsoModel'
import { SsoTextInput } from './SsoTextInput'

/**
 * The SAML 2.0 half of Organization › Single sign-on (F20): what to register
 * at the identity provider (tripl's entity ID, ACS URL and metadata), the
 * IdP's own values (pasted metadata or typed by hand) and the saved signing
 * certificates with their fingerprints and expiry.
 */
export function OrgSsoSamlCard({
  org,
  config,
  draft,
  setValue,
  onImported,
}: {
  org: string
  config: SsoConfig
  draft: SsoDraft
  setValue: (field: SsoTextField, value: string) => void
  /** Pasted metadata was read: its values go into the draft, not yet saved. */
  onImported: (values: SamlMetadataImport) => void
}) {
  const sp = samlSpValues(window.location.origin, org, config)
  const fieldProps = { config, draft, setValue }
  const certsEdited = draft.saml_idp_certs !== undefined
  const nameIdFormat = ssoDisplayValue(config, draft, 'saml_name_id_format')
  // A format saved through the API that the page does not list stays choosable.
  const nameIdOptions = SAML_NAME_ID_FORMATS.some((option) => option.value === nameIdFormat)
    ? SAML_NAME_ID_FORMATS
    : [...SAML_NAME_ID_FORMATS, { value: nameIdFormat, label: nameIdFormat }]

  return (
    <>
      <SCard
        title="Register tripl at your identity provider"
        description="Create a SAML 2.0 application at your IdP with these values, or give it the metadata URL. tripl does not sign its requests, and IdP-initiated sign-in is not supported."
      >
        <InfoRow label="Entity ID (audience)" value={sp.entityId} />
        <InfoRow label="ACS URL (HTTP-POST)" value={sp.acsUrl} />
        <InfoRow label="Metadata URL" value={sp.metadataUrl} last />
      </SCard>

      <SamlMetadataImportCard org={org} onImported={onImported} />

      <SCard
        title="Identity provider"
        description="The IdP must sign every assertion (SHA-256 or stronger) and must not encrypt it. The email is taken from the NameID, or from the attribute named below."
      >
        <SsoTextInput
          {...fieldProps}
          field="saml_idp_entity_id"
          label="IdP entity ID"
          placeholder="e.g. https://idp.example.com/saml"
          hint="The IdP's issuer, exactly as it appears in its assertions."
        />
        <SsoTextInput
          {...fieldProps}
          field="saml_idp_sso_url"
          label="SSO URL"
          placeholder="e.g. https://idp.example.com/sso/saml"
          hint="The IdP's single sign-on address for the HTTP-Redirect binding. https only."
        />
        <SsoTextInput
          {...fieldProps}
          field="saml_idp_certs"
          label="Signing certificates"
          multiline
          placeholder={'-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----'}
          hint="PEM. Paste the new certificate under the current one while your IdP rotates its key; an assertion signed by either is accepted."
        />
        <SamlCertList certs={config.saml_cert_info ?? []} edited={certsEdited} />
        <Field label="NameID format" hint="What tripl asks the IdP to send as the subject.">
          <NativeSelect
            width="fill"
            value={nameIdFormat}
            onChange={(next) => setValue('saml_name_id_format', next)}
            options={nameIdOptions}
          />
        </Field>
        <SsoTextInput
          {...fieldProps}
          field="saml_email_attribute"
          label="Email attribute"
          placeholder="Blank: the NameID is the email"
          hint="Optional. The attribute that carries the email address, for example email or http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress. Needed when the NameID is not an email address."
          last
        />
      </SCard>
    </>
  )
}

function SamlMetadataImportCard({
  org,
  onImported,
}: {
  org: string
  onImported: (values: SamlMetadataImport) => void
}) {
  const [xml, setXml] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const importMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (text: string) => ssoApi.importSamlMetadata(org, text),
    onSuccess: (values) => {
      onImported(values)
      setXml('')
      setNote('Filled in from the metadata. Review the values below, then save.')
    },
  })
  return (
    <SCard
      title="Import IdP metadata"
      description="Paste the metadata XML your IdP offers for download to fill in its entity ID, SSO URL and certificates. tripl only reads what you paste; it does not fetch URLs."
    >
      <Field label="Metadata XML" stacked last>
        <TextArea
          value={xml}
          onChange={(next) => {
            setXml(next)
            setNote(null)
            importMut.reset()
          }}
          placeholder={'<EntityDescriptor entityID="https://idp.example.com/saml" …>'}
          rows={4}
          autoGrow
          mono
        />
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="outline"
            disabled={!xml.trim() || importMut.isPending}
            onClick={() => importMut.mutate(xml)}
          >
            {importMut.isPending ? 'Importing…' : 'Import'}
          </Button>
          {note && (
            <span role="status" className="text-body-sm text-fg-muted">
              {note}
            </span>
          )}
          {importMut.isError && (
            <span role="alert" className="text-body-sm text-danger">
              {getErrorMessage(importMut.error)}
            </span>
          )}
        </div>
      </Field>
    </SCard>
  )
}

const EXPIRY_TEXT = {
  expired: { label: 'Expired', className: 'text-danger' },
  expiring: { label: 'Expires soon', className: 'text-(--warning)' },
  valid: { label: 'Valid', className: 'text-fg-muted' },
  unknown: { label: 'Expiry unknown', className: 'text-fg-muted' },
} as const

/** The saved certificates as the server read them. */
function SamlCertList({ certs, edited }: { certs: readonly SamlCertInfo[]; edited: boolean }) {
  const headingId = useId()
  if (certs.length === 0 && !edited) return null
  return (
    <div
      role="group"
      aria-labelledby={headingId}
      className="px-4 py-3"
      style={{ borderBottom: '1px solid var(--border-subtle)' }}
    >
      <p id={headingId} className="m-0 text-body-sm font-medium text-fg">
        Saved certificates
      </p>
      {edited && (
        <p className="m-0 mt-1 text-caption text-fg-tertiary">
          These are the saved certificates. Save to check the ones you pasted.
        </p>
      )}
      {certs.length === 0 ? (
        <p className="m-0 mt-1 text-body-sm text-fg-tertiary">None saved yet.</p>
      ) : (
        <ul className="m-0 mt-2 list-none space-y-2 p-0">
          {certs.map((cert) => {
            const state = EXPIRY_TEXT[certExpiryState(cert.not_after)]
            const expires = formatDate(cert.not_after)
            return (
              <li key={cert.fingerprint_sha256} className="min-w-0 text-body-sm">
                <div className="truncate text-fg" title={cert.subject}>
                  {cert.subject || 'No subject'}
                </div>
                <div className="mono break-all text-caption text-fg-tertiary">
                  SHA-256 {formatFingerprint(cert.fingerprint_sha256)}
                </div>
                <div className={`text-caption ${state.className}`}>
                  {state.label}
                  {expires ? ` · ${state.label === 'Expired' ? 'expired' : 'expires'} ${expires}` : ''}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
