import { Button } from '@/components/ui/button'
import { Field, NativeSelect, SCard, TextArea, ToggleRow } from '@/components/settings/kit'
import { FIELD_COPY, PHOTO_BACKEND_OPTIONS } from '@/pages/settings-service/fieldCopy'
import { InactiveGroup } from '@/pages/settings-service/ServiceSettingsPrimitives'
import {
  STORAGE_GROUP,
  clearGroup,
  displayValue,
  groupOwned,
  groupWarning,
  mimeListError,
  type OrgDraft,
} from './orgSettingsModel'
import { InheritHint, OrgFieldBadge, OrgTextField, type OrgFieldProps } from './OrgSettingsPrimitives'
import { formatNumber } from '@/lib/format'

/**
 * Organization › Photos (F20 PR11): where the organization's event photos are
 * written, and what an upload may be. Backend, bucket, service-account JSON,
 * public URLs and URL lifetime are one group: an own bucket is only ever
 * written with the organization's own key. The size cap is at most the
 * platform's and the content types a subset of the platform's list. The
 * server paths (local directory, the platform's credentials file) are not
 * shown: they are the platform's alone, set under Platform › Storage, and the
 * page says so where they would be. Labels, units and backend choices are
 * Platform › Storage's (fieldCopy.ts).
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
  const backendOptions = PHOTO_BACKEND_OPTIONS.map(option =>
    option.value === 'local' && !localAllowed
      ? { ...option, label: `${option.label} (not on a hosted platform)`, disabled: true }
      : option,
  )
  const credentialsConfigured = settings.storage.gcs_photo_credentials_configured
  // The bucket rows stay editable on the disk backend (preparing a bucket
  // before switching is valid), but faded, and the Backend row says why.
  const onDisk = backend === 'local'
  const diskNote = onDisk
    ? organizationScope
      ? "Photos go to the server's disk. The bucket fields below are not used until you switch."
      : "Photos go to the server's disk, in the directory set under Platform › Storage. The bucket fields below are not used until you switch."
    : null

  return (
    <>
      <SCard
        title="Where photos are stored"
        description={
          organizationScope
            ? "Backend, bucket, key, public URLs and URL lifetime are one group: once this organization sets any of them its photos go to its own storage, and the platform's credentials are never used on it. Without it, photos use the platform's storage."
            : "The platform's own store: organizations without storage of their own use it."
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
          label={FIELD_COPY.photo_storage_backend.label}
          labelRight={<OrgFieldBadge settings={settings} section={section} field="photo_storage_backend" />}
          hint={
            <>
              {diskNote}
              {diskNote && organizationScope ? ' ' : null}
              <InheritHint {...props} field="photo_storage_backend" grouped />
            </>
          }
        >
          <NativeSelect
            value={backend}
            onChange={value => setField('photo_storage_backend', value)}
            options={backendOptions}
            width="fill"
          />
        </Field>
        <InactiveGroup inactive={onDisk}>
          <OrgTextField
            {...props}
            field="gcs_photo_bucket"
            label={FIELD_COPY.gcs_photo_bucket.label}
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
              labelRight={
                <OrgFieldBadge settings={settings} section={section} field="gcs_photo_credentials_json" />
              }
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
              hint="The path of the platform’s key file (GCS credentials path), or GCS_PHOTO_CREDENTIALS_PATH on the server. Without one, the server’s own identity is used."
              htmlFor={false}
            >
              <p className="m-0 text-body-sm text-fg-tertiary">Set under Platform › Storage.</p>
            </Field>
          )}
          <ToggleRow
            label={FIELD_COPY.gcs_photo_public.label}
            labelRight={<OrgFieldBadge settings={settings} section={section} field="gcs_photo_public" />}
            hint={FIELD_COPY.gcs_photo_public.hint}
            value={displayValue(settings, draft, section, 'gcs_photo_public') === true}
            onChange={value => setField('gcs_photo_public', value)}
          />
          <OrgTextField
            {...props}
            field="gcs_photo_signed_url_ttl_seconds"
            label={FIELD_COPY.gcs_photo_signed_url_ttl_seconds.label}
            number
            suffix={FIELD_COPY.gcs_photo_signed_url_ttl_seconds.suffix}
            grouped
            last
          />
        </InactiveGroup>
      </SCard>
      <SCard
        title="Uploads"
        description={
          organizationScope
            ? 'What an upload to this organization’s events may be. Lower than the platform’s limits, never higher.'
            : 'The platform’s limits: every organization’s maximum.'
        }
      >
        <OrgTextField
          {...props}
          field="photo_max_size_mb"
          label={FIELD_COPY.photo_max_size_mb.label}
          number
          suffix={FIELD_COPY.photo_max_size_mb.suffix}
          hint={
            organizationScope
              ? `Platform maximum: ${formatNumber(settings.ceilings.photo_max_size_mb)} MB.`
              : undefined
          }
        />
        <Field
          label={FIELD_COPY.photo_allowed_mime.label}
          labelRight={<OrgFieldBadge settings={settings} section={section} field="photo_allowed_mime" />}
          hint={
            <>
              {FIELD_COPY.photo_allowed_mime.hint}
              {organizationScope && ` The platform allows: ${operatorMime.join(', ')}.`}{' '}
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
