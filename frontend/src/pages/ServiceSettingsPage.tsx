import { PageHeader } from '@/components/primitives/page-header'
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { serviceSettingsApi } from '@/api/serviceSettings'
import type { ServiceSettingsSectionKey } from './serviceSettingsTabs'
import { useAuth } from '@/components/auth-context'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ErrorState } from '@/components/error-state'
import { SettingsSaveBar } from '@/components/settings/kit'
import { Card, CardContent } from '@/components/ui/card'
import { useConfirm } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import type { ServiceSettings, ServiceSettingsUpdate } from '@/types'

import { AiSection } from './settings-service/AiSection'
import { EmailSection } from './settings-service/EmailSection'
import { ObservabilitySection } from './settings-service/ObservabilitySection'
import { RuntimeSection } from './settings-service/RuntimeSection'
import { SecuritySection } from './settings-service/SecuritySection'
import {
  InstanceSettingsSkeleton,
  ResetSectionCard,
} from './settings-service/ServiceSettingsPrimitives'
import { StorageSection } from './settings-service/StorageSection'
import { SystemCard } from './settings-service/SystemCard'
import {
  type EditableSettings,
  type SecretDrafts,
  type SecretField,
  type SectionKey,
  EMPTY_SECRET_DRAFTS,
  SECTION_LABELS,
  SOURCE_LEGEND,
  adoptSection,
  adoptSectionKeepingEdits,
  applyNote,
  buildUpdate,
  clearSecretConfirm,
  clearSectionSecrets,
  dirtySections,
  editableFromSettings,
  hasUpdate,
  lockoutRisks,
  overrideCount,
  pickSection,
  resetConfirm,
  resetPayload,
  updateHasInvalidNumber,
} from './settings-service/serviceSettingsHelpers'
import { isPlatformAdmin } from '@/lib/permissions'
import {
  aiStatusRootKey,
  authStatusKey,
  inheritsPlatformSettings,
  serviceSettingsKey,
} from '@/lib/queryKeys'

const UNSAVED_MESSAGE =
  'Platform settings you edited here have not been saved. Leaving this page drops them — anything typed into a prompt or a field is gone.'

/**
 * What one PATCH to the settings endpoint is doing.
 *
 * All three send the same request and get the same whole-settings response, but
 * they may not adopt it the same way. Every one of them writes a single
 * section — Save included: it used to send the whole form, so a Security edit
 * left behind while the owner moved on to AI went out with AI's Save and
 * nothing on the AI page said so — and each must leave the other five
 * sections' unsaved edits standing. Clear additionally keeps the edits in its
 * own section.
 */
type SettingsWrite =
  | { kind: 'save'; section: SectionKey; update: ServiceSettingsUpdate }
  | { kind: 'reset'; section: SectionKey }
  | { kind: 'clear-secret'; group: 'ai' | 'email'; field: SecretField }

function writeSection(write: SettingsWrite): SectionKey {
  return write.kind === 'clear-secret' ? write.group : write.section
}

function writesAi(write: SettingsWrite): boolean {
  return writeSection(write) === 'ai'
}

function payloadFor(write: SettingsWrite): ServiceSettingsUpdate {
  if (write.kind === 'clear-secret') {
    return { [write.group]: { [write.field]: null } } as ServiceSettingsUpdate
  }
  return write.kind === 'save' ? write.update : resetPayload(write.section)
}

export default function ServiceSettingsSection({
  section,
}: {
  section: ServiceSettingsSectionKey
}) {
  const { user } = useAuth()
  // The Platform console (F20 PR9): every section is the operator's. An
  // organization's own mail, AI and limits are Organization › Email, AI, Limits.
  const platformAdmin = isPlatformAdmin(user)
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()
  const { registerUnsaved } = useUnsavedChanges()
  const [form, setForm] = useState<EditableSettings | null>(null)
  const [secretDrafts, setSecretDrafts] = useState<SecretDrafts>(EMPTY_SECRET_DRAFTS)
  const [hydratedSettings, setHydratedSettings] = useState<ServiceSettings | null>(null)

  const settingsQuery = useQuery({
    queryKey: serviceSettingsKey(),
    queryFn: serviceSettingsApi.get,
    enabled: platformAdmin,
    // Rendered below as an ErrorState with a retry.
    meta: SILENT_ERROR_META,
  })

  if (settingsQuery.data && hydratedSettings !== settingsQuery.data) {
    setHydratedSettings(settingsQuery.data)
    setForm(editableFromSettings(settingsQuery.data))
    setSecretDrafts(EMPTY_SECRET_DRAFTS)
  }

  const saveMut = useMutation({
    mutationFn: (write: SettingsWrite) =>
      serviceSettingsApi.update(payloadFor(write)),
    // Shown in the sticky save row.
    meta: SILENT_ERROR_META,
    onSuccess: (data, write) => {
      qc.setQueryData(serviceSettingsKey(), data)
      setHydratedSettings(data)
      // Whether a project shows its AI buttons is cached for five minutes
      // (useAiStatus); without this an owner who just turned AI on went back
      // to a project and still saw it off.
      if (writesAi(write)) void qc.invalidateQueries({ queryKey: aiStatusRootKey() })
      // /auth/status reports whether email is configured, which the Security
      // section and the sign-in page's password reset read.
      void qc.invalidateQueries({ queryKey: authStatusKey() })
      // Organizations inherit these values and are capped by them: their settings views and the
      // scan form's row-cap hints would otherwise check against the old ceiling for minutes.
      void qc.invalidateQueries({ predicate: query => inheritsPlatformSettings(query.queryKey) })
      // A write settles only what it wrote. `form` spans all six sections, so
      // replacing it here threw away an unsaved prompt or field in a section
      // this action never touched.
      if (write.kind === 'save' || write.kind === 'reset') {
        setForm(current => (current ? adoptSection(current, data, write.section) : current))
        setSecretDrafts(current => clearSectionSecrets(current, write.section))
        return
      }
      setForm(current => (current ? adoptSectionKeepingEdits(current, data, write.group) : current))
      setSecretDrafts(current => ({ ...current, [write.field]: '' }))
    },
  })

  const update = useMemo(
    () => buildUpdate(form, settingsQuery.data, secretDrafts),
    [form, settingsQuery.data, secretDrafts],
  )
  const dirty = hasUpdate(update)
  const activeSection: SectionKey | null = section === 'system' ? null : section
  const sectionUpdate = useMemo(
    () => (activeSection ? pickSection(update, activeSection) : {}),
    [update, activeSection],
  )
  const sectionDirty = hasUpdate(sectionUpdate)
  const dirtyKeys = dirtySections(update)
  const otherDirty = dirtyKeys.filter(key => key !== activeSection)
  // Only what this Save would send: an env value the backend itself accepted
  // (AI_TIMEOUT_SECONDS=0, say) must not block an unrelated edit.
  const sectionInvalid = activeSection !== null && updateHasInvalidNumber(sectionUpdate, activeSection)
  // One string so the effect below re-registers only when the set changes.
  const dirtyPathKey = dirtyKeys.map(key => `instance/${key}`).join('|')

  // buildUpdate spans every section, and switching between two instance
  // sections keeps this component mounted, so only leaving the instance group
  // actually loses the draft.
  useEffect(() => {
    registerUnsaved(
      dirty
        ? {
            keptBy: path => path.startsWith('instance/'),
            message: UNSAVED_MESSAGE,
            // Feeds the rail's per-section "unsaved changes" marker.
            dirtyPaths: dirtyPathKey.split('|'),
          }
        : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, dirtyPathKey, registerUnsaved])

  const setField = (section: SectionKey, field: string, value: string | number | boolean) => {
    setForm(current => {
      if (!current) return current
      return {
        ...current,
        [section]: {
          ...current[section],
          [field]: value,
        },
      } as EditableSettings
    })
  }

  // Both of these write straight through to the server — no Save step, no undo
  // (the backend pops the override permanently), and on Security a reset can
  // reopen public signup. They are gated the way every other destructive action
  // in the app is.
  const resetSection = async (target: SectionKey) => {
    const sectionDraft = Object.keys(update[target] ?? {}).length > 0
    const ok = await confirm({ ...resetConfirm(target, sectionDraft), variant: 'danger' })
    if (ok) saveMut.mutate({ kind: 'reset', section: target })
  }

  const clearSecret = async (group: 'ai' | 'email', field: SecretField) => {
    const fieldDraft = secretDrafts[field].trim().length > 0
    const ok = await confirm({ ...clearSecretConfirm(field, fieldDraft), variant: 'danger' })
    if (ok) saveMut.mutate({ kind: 'clear-secret', group, field })
  }

  // Security changes that can lock people out or break this app are confirmed
  // the way Reset and Clear are; everything else saves directly.
  const save = async () => {
    if (!activeSection) return
    const risks = lockoutRisks(sectionUpdate)
    if (risks.length > 0) {
      const ok = await confirm({
        title: 'Save changes that can lock people out?',
        message: `${risks.join(' ')} Make sure you can still reach this instance before saving.`,
        confirmLabel: 'Save anyway',
        variant: 'danger',
      })
      if (!ok) return
    }
    saveMut.mutate({ kind: 'save', section: activeSection, update: sectionUpdate })
  }

  // Discards this section only, like Save.
  const discard = () => {
    const saved = settingsQuery.data
    if (!saved || !activeSection) return
    setForm(existing => (existing ? adoptSection(existing, saved, activeSection) : existing))
    setSecretDrafts(existing => clearSectionSecrets(existing, activeSection))
  }

  if (!platformAdmin) {
    return (
      <div className="max-w-3xl">
        <Card>
          <CardContent>
            <PageHeader title="Platform settings" />
            <p className="mt-2 text-body text-fg-tertiary">
              Platform admin is required to view or change platform settings: they configure the
              server itself and the defaults every organization inherits.
            </p>
          </CardContent>
        </Card>
      </div>
    )
  }

  // The error check has to come first: a failed GET leaves `data` undefined,
  // and the loading branch used to catch that and show "Loading…" forever.
  if (settingsQuery.isError && !settingsQuery.data) {
    return (
      <ErrorState
        title="Couldn't load platform settings"
        error={settingsQuery.error}
        onRetry={() => {
          void settingsQuery.refetch()
        }}
      />
    )
  }

  if (!settingsQuery.data || !form) {
    return <InstanceSettingsSkeleton />
  }

  const settings = settingsQuery.data

  return (
    <div className="min-w-0 space-y-5">
      {dialog}
      {section !== 'system' && (
        // The badge legend and the fallback rule, once, above the fields
        // rather than inside the sticky bar.
        <p className="m-0 text-body-sm text-fg-tertiary">
          {SOURCE_LEGEND}
        </p>
      )}
      {section !== 'system' && (
        // The one settings save model: the kit's sticky bar, shared
        // with Project · General. The only Save control used to be a
        // non-sticky first child of the scrolling pane, so the AI page's three
        // prompt textareas were all edited with it off-screen.
        <SettingsSaveBar
          note={applyNote(section)}
          warning={
            otherDirty.length > 0
              ? `Also unsaved: ${otherDirty.map(key => SECTION_LABELS[key]).join(', ')}. Save changes here saves ${SECTION_LABELS[section]} only.`
              : undefined
          }
          // The mutation is shared by every section, but its error belongs
          // to the one it wrote: a Security 422 is not an AI failure.
          error={
            saveMut.isError && saveMut.variables && writeSection(saveMut.variables) === activeSection
              ? getErrorMessage(saveMut.error)
              : undefined
          }
          dirty={sectionDirty}
          invalid={sectionInvalid}
          pending={saveMut.isPending}
          onDiscard={discard}
          onSave={() => void save()}
        />
      )}

      {section === 'runtime' && (
        <RuntimeSection
          form={form}
          settings={settings}
          setField={setField}
          platformAdmin={platformAdmin}
        />
      )}

      {section === 'email' && (
        <EmailSection
          form={form}
          settings={settings}
          secretDrafts={secretDrafts}
          setField={setField}
          setSecretDrafts={setSecretDrafts}
          saving={saveMut.isPending}
          onClearSecret={(group, field) => void clearSecret(group, field)}
        />
      )}

      {section === 'ai' && (
        <AiSection
          form={form}
          settings={settings}
          secretDrafts={secretDrafts}
          setField={setField}
          setSecretDrafts={setSecretDrafts}
          saving={saveMut.isPending}
          onClearSecret={(group, field) => void clearSecret(group, field)}
          platformAdmin={platformAdmin}
        />
      )}

      {section === 'security' && (
        <SecuritySection form={form} settings={settings} setField={setField} />
      )}

      {section === 'storage' && (
        <StorageSection
          form={form}
          settings={settings}
          setField={setField}
          platformAdmin={platformAdmin}
        />
      )}

      {section === 'observability' && (
        <ObservabilitySection form={form} settings={settings} setField={setField} />
      )}

      {section === 'system' && settings.system && <SystemCard system={settings.system} />}

      {section !== 'system' && (
        <ResetSectionCard
          section={section}
          overrides={overrideCount(settings, section)}
          onReset={() => void resetSection(section)}
          resetting={saveMut.isPending}
        />
      )}
    </div>
  )
}
