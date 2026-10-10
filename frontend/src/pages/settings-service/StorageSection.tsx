import type { ServiceSettings } from '@/types'
import { Field, RadioCards, SCard, TextArea, TextInput, ToggleRow } from '@/components/settings/kit'
import { FIELD_COPY, PHOTO_BACKEND_OPTIONS, inactiveBackendNote } from './fieldCopy'
import { InactiveGroup, NumberSettingInput, OperatorFields, SourceBadge } from './ServiceSettingsPrimitives'
import type { EditableSettings, SectionKey } from './serviceSettingsHelpers'
import { sourceFor } from './serviceSettingsHelpers'

/**
 * Platform › Storage: where event photos are written, and what an upload may
 * be. Labels, units and the backend choices are the ones Organization › Photos
 * shows for the same values (fieldCopy.ts), and the upload limits sit on their
 * own "Uploads" card there and here.
 */
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
   * Every storage field but the content-type allow-list is operator-only
   * (backend `OPERATOR_FIELDS`): one value serves every organization until each
   * has its own.
   */
  platformAdmin: boolean
}) {
  // Both backend cards stay editable (an owner may prepare GCS before
  // switching), but the one not selected above says so: with both always
  // looking live it was unclear which fields mattered.
  const backend = form.storage.photo_storage_backend
  return (
    <>
      <SCard title="Where photos are stored">
        <OperatorFields locked={!platformAdmin}>
        <Field
          label={FIELD_COPY.photo_storage_backend.label}
          labelRight={
            <SourceBadge source={sourceFor(settings, 'storage', 'photo_storage_backend')} />
          }
          stacked
          last
        >
          <RadioCards
            groupLabel="Photo storage backend"
            value={backend}
            onChange={value => setField('storage', 'photo_storage_backend', value)}
            options={PHOTO_BACKEND_OPTIONS}
            columns={2}
          />
        </Field>
        </OperatorFields>
      </SCard>

      <SCard
        title={PHOTO_BACKEND_OPTIONS[0].label}
        description={backend === 'local' ? undefined : inactiveBackendNote(backend)}
      >
        {/* Faded as well as described: the note alone left every field looking
            live. */}
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
        title={PHOTO_BACKEND_OPTIONS[1].label}
        description={backend === 'gcs' ? undefined : inactiveBackendNote(backend)}
      >
        <OperatorFields locked={!platformAdmin}>
        <InactiveGroup inactive={backend !== 'gcs'}>
        <Field
          label={FIELD_COPY.gcs_photo_bucket.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'gcs_photo_bucket')} />}
        >
          <TextInput
            value={form.storage.gcs_photo_bucket}
            onChange={value => setField('storage', 'gcs_photo_bucket', value)}
            mono
          />
        </Field>
        {/* The platform's key is a file on the server, not its content: an
            organization's own bucket takes its key's JSON on Organization ›
            Photos instead. */}
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
        <ToggleRow
          label={FIELD_COPY.gcs_photo_public.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'gcs_photo_public')} />}
          hint={FIELD_COPY.gcs_photo_public.hint}
          value={form.storage.gcs_photo_public}
          onChange={value => setField('storage', 'gcs_photo_public', value)}
        />
        <Field
          label={FIELD_COPY.gcs_photo_signed_url_ttl_seconds.label}
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
            suffix={FIELD_COPY.gcs_photo_signed_url_ttl_seconds.suffix}
          />
        </Field>
        </InactiveGroup>
        </OperatorFields>
      </SCard>

      <SCard
        title="Uploads"
        description="What an upload may be: every organization's maximum. An organization may set lower limits of its own."
      >
        <OperatorFields locked={!platformAdmin}>
        <Field
          label={FIELD_COPY.photo_max_size_mb.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'photo_max_size_mb')} />}
        >
          <NumberSettingInput
            section="storage"
            field="photo_max_size_mb"
            value={form.storage.photo_max_size_mb}
            saved={settings.storage.photo_max_size_mb}
            setField={setField}
            suffix={FIELD_COPY.photo_max_size_mb.suffix}
          />
        </Field>
        </OperatorFields>
        <Field
          label={FIELD_COPY.photo_allowed_mime.label}
          labelRight={<SourceBadge source={sourceFor(settings, 'storage', 'photo_allowed_mime')} />}
          hint={FIELD_COPY.photo_allowed_mime.hint}
          last
        >
          <TextArea
            value={form.storage.photo_allowed_mime}
            onChange={value => setField('storage', 'photo_allowed_mime', value)}
            rows={2}
            mono
          />
        </Field>
      </SCard>
    </>
  )
}
