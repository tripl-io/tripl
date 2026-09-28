import { Button } from '@/components/ui/button'
import { Field, NativeSelect, SCard, TextArea, ToggleRow } from '@/components/settings/kit'
import {
  STORAGE_GROUP,
  clearGroup,
  displayValue,
  groupOwned,
  groupWarning,
  mimeListError,
  sourceOf,
  type OrgDraft,
} from './orgSettingsModel'
import { InheritHint, OrgSourceBadge, OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'

/**
 * Organization › Storage (F20 PR11): where the organization's event photos are
 * written, and what an upload may be. Backend, bucket, service-account JSON,
 * public URLs and URL lifetime are one group: an own bucket is only ever
 * written with the organization's own key. The size cap is at most the
 * operator's and the content types a subset of the operator's list. The
 * server paths (local directory, the operator's credentials file) are not
 * shown: they are the operator's alone.
 */
export default function OrgStorageFields({
  settings,
  draft,
  setField,
  setDraft,
  saving,
}: OrgFieldProps & {
  setDraft: (next: OrgDraft) => void
  saving: boolean
}) {
  const section = 'storage' as const
  const props = { settings, draft, setField, section }
  const organizationScope = settings.scope === 'organization'
  const owned = organizationScope && groupOwned(settings, section, STORAGE_GROUP)
  const warning = groupWarning(settings, draft, section)
  const backend = String(displayValue(settings, draft, section, 'photo_storage_backend'))
  const localAllowed = settings.storage_limits.local_backend_allowed
  const operatorMime = settings.storage_limits.operator_allowed_mime
  const mimeDraft = draft.photo_allowed_mime
  const mimeError =
    organizationScope && mimeDraft !== undefined ? mimeListError(mimeDraft, operatorMime) : null
  const backendOptions = [
    { value: 'gcs', label: 'Google Cloud Storage bucket' },
    {
      value: 'local',
      label: localAllowed ? "The server's disk" : "The server's disk (not on a hosted platform)",
      disabled: !localAllowed,
    },
  ]
  const credentialsConfigured = settings.storage.gcs_photo_credentials_configured

  return (
    <>
      <SCard
        title="Where photos are stored"
        description={
          organizationScope
            ? "Backend, bucket, key, public URLs and URL lifetime are one group: once this organization sets any of them its photos go to its own storage, and the platform's credentials are never used on it. Without it, photos use the platform's storage."
            : "The platform's own store: organizations without storage of their own use it. Takes effect when the server restarts."
        }
        footer={
          owned ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setDraft(clearGroup(draft, settings, section, STORAGE_GROUP))}
              disabled={saving}
            >
              Use the platform&rsquo;s storage
            </Button>
          ) : undefined
        }
      >
        {warning && (
          <p role="note" className="m-0 px-4 pt-2.5 text-caption text-(--warning)">
            {warning.starting &&
              'Saving moves new photos to this organization’s own storage: fields you leave as they are take the built-in defaults, not the platform’s. Photos already stored stay where they are. '}
            {warning.missingSecret &&
              backend === 'gcs' &&
              'Add the bucket’s service-account JSON key: the platform’s credentials are never used on it.'}
          </p>
        )}
        <Field
          label="Backend"
          labelRight={<OrgSourceBadge source={sourceOf(settings, section, 'photo_storage_backend')} />}
          hint={<InheritHint {...props} field="photo_storage_backend" grouped />}
        >
          <NativeSelect
            value={backend}
            onChange={value => setField('photo_storage_backend', value)}
            options={backendOptions}
            width="fill"
          />
        </Field>
        <OrgTextField
          {...props}
          field="gcs_photo_bucket"
          label="GCS bucket"
          grouped
          placeholder={organizationScope ? 'e.g. acme-tripl-photos' : undefined}
          hint={
            organizationScope && !owned
              ? 'Blank while this organization uses the platform’s storage.'
              : undefined
          }
        />
        {organizationScope ? (
          <Field
            label="Service-account JSON key"
            labelRight={<OrgSourceBadge source={sourceOf(settings, section, 'gcs_photo_credentials_json')} />}
            hint="The key file’s content, with write access to the bucket. Stored encrypted and never shown again."
          >
            <TextArea
              value={String(displayValue(settings, draft, section, 'gcs_photo_credentials_json'))}
              onChange={value => setField('gcs_photo_credentials_json', value)}
              placeholder={
                credentialsConfigured
                  ? 'Configured — leave blank to keep'
                  : '{ "type": "service_account", … }'
              }
              rows={4}
              mono
            />
          </Field>
        ) : (
          <Field
            label="GCS credentials"
            hint="The platform’s key is GCS_PHOTO_CREDENTIALS_PATH on the server (or its own identity), not a setting."
          >
            <p className="m-0 text-body-sm text-fg-tertiary">Set on the server.</p>
          </Field>
        )}
        <ToggleRow
          label="Public URLs"
          labelRight={<OrgSourceBadge source={sourceOf(settings, section, 'gcs_photo_public')} />}
          hint="On, photos load from the bucket’s public address; off, from signed links that expire."
          value={displayValue(settings, draft, section, 'gcs_photo_public') === true}
          onChange={value => setField('gcs_photo_public', value)}
        />
        <OrgTextField
          {...props}
          field="gcs_photo_signed_url_ttl_seconds"
          label="Signed link lifetime"
          number
          suffix="seconds"
          grouped
          last
        />
      </SCard>
      <SCard
        title="Uploads"
        description={
          organizationScope
            ? 'What an upload to this organization’s events may be. Lower than the platform’s limits, never higher.'
            : 'The platform’s limits: every organization’s maximum. Takes effect when the server restarts.'
        }
      >
        <OrgTextField
          {...props}
          field="photo_max_size_mb"
          label="Largest photo"
          number
          suffix="MB"
          hint={
            organizationScope
              ? `Operator maximum: ${settings.ceilings.photo_max_size_mb.toLocaleString('en-US')} MB.`
              : undefined
          }
        />
        <Field
          label="Allowed content types"
          labelRight={<OrgSourceBadge source={sourceOf(settings, section, 'photo_allowed_mime')} />}
          hint={
            <>
              Comma-separated.
              {organizationScope && ` The operator allows: ${operatorMime.join(', ')}.`}{' '}
              <InheritHint {...props} field="photo_allowed_mime" />
            </>
          }
          last
        >
          <TextArea
            value={String(displayValue(settings, draft, section, 'photo_allowed_mime'))}
            onChange={value => setField('photo_allowed_mime', value)}
            rows={2}
            mono
          />
          {mimeError && <p className="mt-1 text-caption text-danger">{mimeError}</p>}
        </Field>
      </SCard>
    </>
  )
}
