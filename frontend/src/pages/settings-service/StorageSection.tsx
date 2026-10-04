import type { ServiceSettings } from '@/types'
import { Field, RadioCards, SCard, TextInput, ToggleRow } from '@/components/settings/kit'
import { InactiveGroup, NumberSettingInput, OperatorFields, SourceBadge } from './ServiceSettingsPrimitives'
import type { EditableSettings, SectionKey } from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'

const PHOTO_STORAGE_BACKEND_OPTIONS = [
  { value: 'local', label: 'Local filesystem' },
  { value: 'gcs', label: 'Google Cloud Storage' },
] as const

export function StorageSection({
  form,
  settings,
  setField,
  platformAdmin,
}: {
  form: EditableSettings
  settings: ServiceSettings
  setField: (section: SectionKey, field: string, value: string | number | boolean) => void
  /**
   * Every storage field but the MIME allow-list is operator-only (backend
   * `OPERATOR_FIELDS`): one value serves every organization until each has its own.
   */
  platformAdmin: boolean
}) {
  // Both backend cards stay editable (an owner may prepare GCS before
  // switching), but the one not selected above says so: with both always
  // looking live it was unclear which fields mattered.
  const backend = form.storage.photo_storage_backend
  const inactiveNote = (active: string) =>
    `Inactive — the backend above is ${active}, so these fields are not used until you switch.`
  return (
    <>
      <SCard title="Backend">
        <OperatorFields locked={!platformAdmin}>
        <Field
          label="Photo storage backend"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'storage', 'photo_storage_backend')} />
          }
          stacked
        >
          <RadioCards
            groupLabel="Photo storage backend"
            value={form.storage.photo_storage_backend}
            onChange={value => setField('storage', 'photo_storage_backend', value)}
            options={PHOTO_STORAGE_BACKEND_OPTIONS}
            columns={2}
          />
        </Field>
        <Field
          label="Photo max size"
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'photo_max_size_mb')} />}
        >
          <NumberSettingInput
            section="storage"
            field="photo_max_size_mb"
            value={form.storage.photo_max_size_mb}
            saved={settings.storage.photo_max_size_mb}
            setField={setField}
            suffix="MB"
          />
        </Field>
        </OperatorFields>
        <Field
          label="Allowed MIME types"
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'photo_allowed_mime')} />}
          last
        >
          <TextInput
            value={form.storage.photo_allowed_mime}
            onChange={value => setField('storage', 'photo_allowed_mime', value)}
            mono
          />
        </Field>
      </SCard>

      <SCard
        title="Local filesystem"
        description={backend === 'local' ? undefined : inactiveNote('Google Cloud Storage')}
      >
        {/* Faded as well as described: the note alone left every field looking
            live (after WS-30). */}
        <OperatorFields locked={!platformAdmin}>
        <InactiveGroup inactive={backend !== 'local'}>
        <Field
          label="Local photo directory"
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'photo_local_dir')} />}
          last
        >
          <TextInput
            value={form.storage.photo_local_dir}
            onChange={value => setField('storage', 'photo_local_dir', value)}
            mono
          />
        </Field>
        </InactiveGroup>
        </OperatorFields>
      </SCard>

      <SCard
        title="Google Cloud Storage"
        description={backend === 'gcs' ? undefined : inactiveNote('Local filesystem')}
      >
        <OperatorFields locked={!platformAdmin}>
        <InactiveGroup inactive={backend !== 'gcs'}>
        <Field
          label="GCS bucket"
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'gcs_photo_bucket')} />}
        >
          <TextInput
            value={form.storage.gcs_photo_bucket}
            onChange={value => setField('storage', 'gcs_photo_bucket', value)}
            mono
          />
        </Field>
        <ToggleRow
          label="GCS public URLs"
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'gcs_photo_public')} />}
          value={form.storage.gcs_photo_public}
          onChange={value => setField('storage', 'gcs_photo_public', value)}
        />
        <Field
          label="GCS credentials path"
          labelRight={
            <SourceBadge source={sourceFor(settings, 'storage', 'gcs_photo_credentials_path')} />
          }
        >
          <TextInput
            value={form.storage.gcs_photo_credentials_path}
            onChange={value => setField('storage', 'gcs_photo_credentials_path', value)}
            mono
          />
        </Field>
        <Field
          label="Signed URL TTL"
          labelRight={
            <SourceBadge
              source={sourceFor(settings, 'storage', 'gcs_photo_signed_url_ttl_seconds')}
            />
          }
          last
        >
          <NumberSettingInput
            section="storage"
            field="gcs_photo_signed_url_ttl_seconds"
            value={form.storage.gcs_photo_signed_url_ttl_seconds}
            saved={settings.storage.gcs_photo_signed_url_ttl_seconds}
            setField={setField}
            suffix="seconds"
          />
        </Field>
        </InactiveGroup>
        </OperatorFields>
      </SCard>
    </>
  )
}
