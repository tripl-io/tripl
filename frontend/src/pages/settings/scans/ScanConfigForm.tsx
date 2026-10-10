import { PageBackLink, PageHeader } from '@/components/primitives/page-header'
import { PageContainer } from '@/components/primitives/page-container'
import { SaveBar } from '@/components/forms/SaveBar'
import { SCard } from '@/components/settings/kit'
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { eventTypesApi } from '@/api/eventTypes'
import { scansApi } from '@/api/scans'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { useProjectDataSources } from '@/hooks/useProjectDataSources'
import type { EventType, ScanConfig } from '@/types'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/error-state'
import {
  AppVersionSection,
  EventNamingSection,
  LimitsSection,
  MetricsDriftSection,
  ScanEssentialsSection,
} from './ScanFormSections'
import { scanFormBlocker, useScanForm, type ScanFormPayload } from './useScanForm'
import { dryRunNameExplosion } from './scanDryRunWarnings'
import { ScanConfigReadView } from './ScanConfigReadView'
import { countOf } from '@/lib/plural'
import { eventTypesKey, platformPresenceKey, scanConfigKey, scanJobsKey, scansKey } from '@/lib/queryKeys'
import { ownerOnlyReason, useIsOwner } from '@/lib/permissions'
import { DisabledReason, ReadOnlyNotice, disabledReasonAria } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'

// ─── Configuration tab (page-style edit, one Save for the whole form) ───
export function ScanConfigurationTab({
  slug,
  scanConfig,
  onDeleted,
  onDirtyChange,
  onDiscard,
}: {
  slug: string
  scanConfig: ScanConfig
  onDeleted: () => void
  /**
   * Throw the edits away. The page remounts this form from the saved config,
   * which is the one reset that cannot miss a field (#247).
   */
  onDiscard?: () => void
  /**
   * Told whether the form holds unsaved edits, and `false` once it unmounts.
   * The page owns the leave guard: it also has to ask before its own tab strip
   * unmounts this form, which no guard in here can see.
   */
  onDirtyChange?: (dirty: boolean) => void
}) {
  const qc = useQueryClient()
  const branchId = useActiveBranchId()
  const { confirm, dialog } = useConfirm()
  const form = useScanForm(slug, scanConfig)
  // Update, preview, replay and delete are all OwnerUserDep: anyone else reads
  // the configuration as a definition list, with no Save.
  const canEdit = useIsOwner()

  const { data: dataSources = [] } = useProjectDataSources()
  const { data: eventTypes = [] } = useQuery({
    queryKey: eventTypesKey(slug, null),
    queryFn: () => eventTypesApi.list(slug, null),
  })

  // What a Save would send, against what the last save (or the page load) sent.
  const payloadSnapshot = JSON.stringify(form.toBackendPayload())
  const [savedSnapshot, setSavedSnapshot] = useState(payloadSnapshot)
  // A non-owner gets the read view below and so is never dirty.
  const dirty = canEdit && payloadSnapshot !== savedSnapshot
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange])

  // The payload travels as the mutation's variables so the saved snapshot is
  // the one this request SENT, not whatever the form holds when it answers
  // (an edit typed while the save was in flight must stay dirty).
  const updateMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (payload: ScanFormPayload) => scansApi.update(slug, scanConfig.id, payload),
    onSuccess: (_saved, payload) => {
      setSavedSnapshot(JSON.stringify(payload))
      // The single-scan read too: a new interval moves the next metrics run.
      void qc.invalidateQueries({ queryKey: scanConfigKey(slug, scanConfig.id) })
      return qc.invalidateQueries({ queryKey: scansKey(slug) })
    },
  })

  const deleteMut = useMutation({
    // Rendered inline in the Danger zone, next to the button that failed.
    meta: SILENT_ERROR_META,
    mutationFn: () => scansApi.del(slug, scanConfig.id),
    onSuccess: () => {
      // The list mounts from cache (staleTime 60s), so without this the deleted
      // scan was still listed there with a Run now that 404s. Drop it
      // from the cache now, then refetch for anything else that changed.
      qc.setQueryData<ScanConfig[]>(scansKey(slug), current =>
        current?.filter(config => config.id !== scanConfig.id),
      )
      qc.removeQueries({ queryKey: scanJobsKey(slug, scanConfig.id) })
      qc.removeQueries({ queryKey: platformPresenceKey(slug, scanConfig.id) })
      qc.removeQueries({ queryKey: scanConfigKey(slug, scanConfig.id) })
      void qc.invalidateQueries({ queryKey: scansKey(slug) })
      onDeleted()
    },
  })

  const handleDelete = async () => {
    const ok = await confirm({
      title: 'Delete scan',
      message: `Delete "${scanConfig.name}"? Stops adding events from this query. Events already in your plan are kept.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate()
  }

  // The reason, not just the fact: a disabled Save with no explanation is how a
  // user ends up believing the form is broken.
  const saveBlocker = scanFormBlocker(form.state)

  // "Saved." only while the form still holds what was saved: it used to stay up
  // after further edits, under every card at once.
  const saveStatus = updateMut.isPending
    ? ''
    : dirty
      ? 'Unsaved changes.'
      : updateMut.isSuccess
        ? 'Saved.'
        : ''

  // A reader gets the definition, not the edit form with every control
  // disabled: live borders, pickers and author hints for someone who can only
  // read (#237 rule 4). After every hook, so their order holds.
  if (!canEdit) {
    return (
      <div className="flex flex-col">
        <ReadOnlyNotice className="mb-5">
          {ownerOnlyReason('change, replay or delete a scan')}
        </ReadOnlyNotice>
        <ScanConfigReadView
          scanConfig={scanConfig}
          dataSources={dataSources}
          eventTypes={eventTypes as EventType[]}
        />
      </div>
    )
  }

  const sectionProps = {
    form,
    slug,
    branchId,
    dataSources,
    eventTypes: eventTypes as EventType[],
    sourceLocked: true,
  }

  return (
    <div className="flex flex-col">
      {dialog}
      {updateMut.isError && (
        <div className="mb-5">
          <ErrorState compact title="Could not save scan" error={updateMut.error} />
        </div>
      )}
      <ScanEssentialsSection {...sectionProps} />
      <EventNamingSection {...sectionProps} />
      <AppVersionSection {...sectionProps} />
      <MetricsDriftSection {...sectionProps} />
      <LimitsSection {...sectionProps} />

      {/* One Save for the whole form. Every card used to carry its own, which
          read as "save this card" while each one sent the entire form — so
          Save under Limits also committed a half-edited query two cards up.
          Sticky, so it is in reach from whichever card was edited. */}
      {/* Only while there is something to say: a clean form showed a grey strip
          holding a disabled Save and no text, which read as an empty footer.
          Once edited it names the state and offers Discard beside Save
          changes, like the other settings forms (#247). The blocker is
          visible text beside Save: a `title` on a disabled button never shows
          (#237). */}
      {(dirty || updateMut.isPending || updateMut.isSuccess) && (
        <SaveBar
          className="mb-5"
          status={
            dirty && saveBlocker ? (
              <DisabledReason id="save-scan" reason={saveBlocker} />
            ) : (
              saveStatus
            )
          }
          statusTone={dirty ? 'warning' : 'muted'}
        >
          {onDiscard && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!dirty || updateMut.isPending}
              onClick={onDiscard}
            >
              Discard
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            onClick={() => updateMut.mutate(form.toBackendPayload())}
            disabled={updateMut.isPending || !dirty || saveBlocker !== null}
            {...(dirty ? disabledReasonAria('save-scan', saveBlocker) : {})}
          >
            {updateMut.isPending ? 'Saving…' : 'Save changes'}
          </Button>
        </SaveBar>
      )}

      <SCard title="Danger zone" tone="danger">
        {/* Only Delete here: Replay re-reads history and deletes nothing, so
            it moved to the page header as a dialog (#247). */}
        <div className="flex items-center gap-[18px] px-4 py-3.5">
          <div className="flex-1">
            <div className="text-body font-medium text-fg">
              Delete scan
            </div>
            <div className="mt-0.5 text-body-sm text-fg-tertiary">
              Stops adding events from this query. Events already in your plan are kept.
            </div>
          </div>
          {/* Bare red in a row; the solid red is the confirm dialog's. */}
          <Button
            type="button"
            variant="danger"
            size="sm"
            disabled={deleteMut.isPending}
            onClick={handleDelete}
          >
            <Trash2 className="size-3" />
            {deleteMut.isPending ? 'Deleting…' : 'Delete'}
          </Button>
        </div>
        {deleteMut.isError && (
          <div className="px-4 pb-3.5">
            <ErrorState compact title="Could not delete scan" error={deleteMut.error} />
          </div>
        )}
      </SCard>
    </div>
  )
}

// ─── New scan — full page at /p/:slug/scans/new (no dialog) ───
export function ScanCreatePage({
  slug,
  onBack,
  onCreated,
}: {
  slug: string
  onBack: () => void
  /** Where to go once the scan exists: its own page, where Run now lives. */
  onCreated: (created: ScanConfig) => void
}) {
  const qc = useQueryClient()
  const branchId = useActiveBranchId()
  const form = useScanForm(slug, null)
  // A typed SQL query and its group rules used to vanish on Back or a reload,
  // with no route to come back to and nothing asking first.
  const [initialSnapshot] = useState(() => JSON.stringify(form.state))
  const unsaved = useUnsavedChangesGuard(JSON.stringify(form.state) !== initialSnapshot)

  const { data: dataSources = [] } = useProjectDataSources()
  const { data: eventTypes = [] } = useQuery({
    queryKey: eventTypesKey(slug, null),
    queryFn: () => eventTypesApi.list(slug, null),
  })

  const createMut = useMutation({
    // Rendered inline below as "Could not create scan".
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      scansApi.create(slug, {
        data_source_id: form.state.dataSourceId,
        ...form.toBackendPayload(),
      }),
    onSuccess: created => {
      qc.invalidateQueries({ queryKey: scansKey(slug) })
      unsaved.release()
      onCreated(created)
    },
  })

  const loaded = Boolean(form.preview)
  const createBlocker = scanFormBlocker(form.state)
  // When the dry run shows the draft would swamp the plan, the line beside
  // Create says what pressing it leads to, instead of the neutral "Creates the
  // scan" it gave a good answer too (#247).
  const explosion = form.dryRunStale ? null : dryRunNameExplosion(form.dryRun)

  const sectionProps = {
    form,
    slug,
    branchId,
    dataSources,
    eventTypes: eventTypes as EventType[],
    sourceLocked: false,
  }

  return (
    <PageContainer width="narrow" className="space-y-0">
      {unsaved.dialog}
      {/* "…to ingest events and roll up metrics" promised monitoring before the
          user had chosen it, and read as a contradiction with Catalog only two
          lines below. What the scan does is now the first question, and the note
          under the radio answers it, so this line only has to say what you are
          pointing at what. */}
      <PageHeader
        className="mb-[18px]"
        eyebrow="Govern · Scan"
        title="New scan"
        description="Point a warehouse query at tripl, and choose what it does with the rows."
        back={<PageBackLink label="Scans" onClick={onBack} />}
      />

      <ScanEssentialsSection {...sectionProps} />
      <EventNamingSection {...sectionProps} />
      <AppVersionSection {...sectionProps} />
      <MetricsDriftSection {...sectionProps} />
      <LimitsSection {...sectionProps} />

      {createMut.isError && (
        <div className="mb-5">
          <ErrorState compact title="Could not create scan" error={createMut.error} />
        </div>
      )}

      {/* Sticky, like the edit form's bar: Create sat under five cards and was
          a full scroll away from the query at the top. */}
      <SaveBar
        status={
          // In Catalog + monitoring the preview is not optional: the time column
          // is chosen from the columns it returns. In Catalog only it is not
          // "optional" either any more — it is how you find out what this scan
          // would put in your plan before you create it.
          // A blocker leads, as visible text, since a disabled button's
          // `title` never shows (#237); the next step stays under it.
          // Muted, not warning: on a form nobody has typed into yet it is the
          // next step, not a fault.
          <>
            <DisabledReason id="create-scan" tone="muted" reason={createBlocker} />
            <span className="block">
              {loaded && explosion
                ? `This scan would add ${countOf(explosion.newEvents, 'event', 'events')} to your plan on its first run.`
                : loaded
                ? 'Creates the scan. Run it from its page when you are ready.'
                : form.state.mode === 'monitoring'
                  ? 'Load a preview to choose a time column and see what this scan would create.'
                  : 'Load a preview to see what this scan would create.'}
            </span>
          </>
        }
      >
        <Button type="button" variant="outline" size="sm" onClick={onBack}>
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={createBlocker !== null || createMut.isPending}
          {...disabledReasonAria('create-scan', createBlocker)}
          onClick={() => createMut.mutate()}
        >
          <Plus className="size-3.5" />
          {createMut.isPending ? 'Creating…' : 'Create scan'}
        </Button>
      </SaveBar>
    </PageContainer>
  )
}
