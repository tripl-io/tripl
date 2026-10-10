import { DEFAULT_ENTITY_COLOR } from '@/types'
import { Panel } from '@/components/settings/kit'
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Trash2 } from 'lucide-react'
import { eventsApi } from '@/api/events'
import { eventTypeOwnersApi } from '@/api/eventTypeOwners'
import { eventTypesApi } from '@/api/eventTypes'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import { UsedBySection } from '@/components/dependencies/UsedBySection'
import { DocFieldNotes, DocNotesSection } from '@/components/docs/DocNotesSection'
import { EventTypeHealthSummary } from './EventTypeHealthSummary'
import { requestPageLeave } from '@/hooks/useUnsavedChangesGuard'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { usePageTitle } from '@/components/shell-chrome-context'
import { EntityNotFound, PageSkeleton, ReadOnlyDefinition, ReadOnlyNotice } from '@/components/states'
import { ErrorState } from '@/components/error-state'
import type { EventType, EventTypeOwner } from '@/types'
import { Button } from '@/components/ui/button'
import { WatchButton } from '@/components/watch-button'
import { Chip } from '@/components/primitives/chip'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { PageContainer } from '@/components/primitives/page-container'
import { PageBackLink, PageHeader } from '@/components/primitives/page-header'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { EVENT_STATUSES } from '@/lib/eventStatus'
import { getErrorMessage } from '@/lib/utils'
import { describeEventTypeDeletionImpact } from './eventTypeDeletionImpact'
import EventsPage from '@/pages/EventsPage'
import {
  eventTypeDeletionImpactKey,
  eventTypeOwnersKey,
  eventTypesKey,
  projectEventTypesKey,
  projectKey,
} from '@/lib/queryKeys'
import {
  ColorPicker,
  FieldsEditor,
  OwnersEditor,
  SField,
  SInput,
  STextarea,
  SaveFooter,
} from './EventTypesTab'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const sensitiveFieldCount = (et: EventType): number =>
  et.field_definitions.filter((f) => f.sensitivity !== 'none').length
const requiredFieldCount = (et: EventType): number =>
  et.field_definitions.filter((f) => f.is_required).length

type DetailTab = 'events' | 'summary' | 'settings'

// In the order they open: Summary is the default, so it comes first.
const TABS: { id: DetailTab; label: string }[] = [
  { id: 'summary', label: 'Summary' },
  { id: 'events', label: 'Events' },
  { id: 'settings', label: 'Settings' },
]

function isDetailTab(value: string | null): value is DetailTab {
  return TABS.some((t) => t.id === value)
}

export function EventTypeDetail({ slug, eventTypeId }: { slug: string; eventTypeId: string }) {
  const navigate = useNavigate()
  const branchId = useActiveBranchId()
  // The tab lives in `?tab=`, so a link can point at an event type's settings
  // and Back from "View events" returns to the tab it left. Replaced
  // rather than pushed: switching tabs is not a navigation worth a Back step.
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const tab: DetailTab = isDetailTab(tabParam) ? tabParam : 'summary'
  const showTab = (next: DetailTab) =>
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev)
        if (next === 'summary') params.delete('tab')
        else params.set('tab', next)
        return params
      },
      { replace: true },
    )
  // The Settings tab can hold the field subpage, whose unsaved-changes guard is
  // the page guard while it is mounted; switching tabs unmounts it, so the
  // switch goes through that guard (asks only when the field has a draft).
  const setTab = (next: DetailTab) => {
    if (next === tab) return
    requestPageLeave(() => showTab(next))
  }

  const { data, isSuccess, isError, error, refetch } = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug, branchId),
    meta: SILENT_ERROR_META,
  })
  const eventTypes = data ?? []

  const et = eventTypes.find((e) => e.id === eventTypeId)
  // The top bar ends on the type, not on "Event types".
  usePageTitle(et?.display_name)
  // Branches deep-copy event types under new ids, so the id in the URL belongs
  // to ONE branch. Switching branch on this page used to end at "Event type not
  // found."; the type is followed by name instead, the identity that
  // survives the copy. Remembered during render, like any value followed from
  // a prop, so the switch can still read the name the page was showing.
  const [lastSeenName, setLastSeenName] = useState<string | null>(null)
  if (et && et.name !== lastSeenName) setLastSeenName(et.name)
  const sameNameOnThisBranch =
    !et && isSuccess && lastSeenName !== null
      ? eventTypes.find((e) => e.name === lastSeenName)
      : undefined
  const redirectTo = sameNameOnThisBranch
    ? projectPath(currentOrgSlug(), slug, `/event-types/${sameNameOnThisBranch.id}${searchParams.toString() ? `?${searchParams.toString()}` : ''}`)
    : null
  useEffect(() => {
    if (redirectTo) navigate(redirectTo, { replace: true })
  }, [navigate, redirectTo])

  const listPath = projectPath(currentOrgSlug(), slug, '/event-types')
  const goBack = () => navigate(listPath)
  const goEvents = () => navigate(projectPath(currentOrgSlug(), slug, `/events/${et?.name ?? 'all'}`))

  // Only a load that never answered replaces the page. A failed refetch keeps
  // the cached type on screen with a line saying so: every save on the Settings
  // tab refetches this list, and unmounting the page on a failed refetch threw
  // away a field draft without asking.
  if (isError && data === undefined) {
    return (
      <div className="space-y-4">
        <PageBackLink label="Event types" to={listPath} />
        <ErrorState
          compact
          title="Couldn't load this event type"
          error={error}
          onRetry={() => { void refetch() }}
          retryLabel="Retry"
        />
      </div>
    )
  }

  // The not-found state with the way back, not a grey sentence (#237).
  if (isSuccess && !et && !sameNameOnThisBranch) {
    return (
      <EntityNotFound
        title="Event type not found"
        description={
          branchId === null
            ? 'This event type does not exist on main. It may have been deleted or renamed.'
            : 'This event type does not exist on the selected branch. It may have been deleted, renamed, or only exist on another branch.'
        }
        back={{ to: listPath, label: 'Back to event types' }}
      />
    )
  }

  // First load: the page's shape, not a blank column (#237).
  if (!et) return redirectTo ? null : <PageSkeleton variant="detail" label="Loading event type…" />

  return (
    <PageContainer className="space-y-3.5">
      {/* The shared page header: the type's name is the page's h1, the
          eyebrow names the parent collection. Its actions wrap under the title
          on a phone. */}
      <PageHeader
        back={<PageBackLink label="Event types" to={listPath} />}
        eyebrow="Plan · Event type"
        title={
          <span className="inline-flex items-center gap-2.5">
            <span
              className="size-3.5 shrink-0 rounded-sm"
              style={{ background: et.color || DEFAULT_ENTITY_COLOR }}
              aria-hidden="true"
            />
            {et.display_name}
          </span>
        }
        titleAddon={
          <>
            <span className="mono text-body-sm text-fg-tertiary">
              {et.name}
            </span>
            <MergeGateChip slug={slug} eventType={et} />
          </>
        }
        description={et.description || undefined}
        // No "Settings" button: it did exactly what the Settings tab beside
        // it does.
        actions={
          <>
            {/* Watched on main only: an event type's branch copy is a draft
                of the same type, and owners are kept on main (#259). */}
            {branchId === null && (
              <WatchButton slug={slug} entityType="event_type" entityId={et.id} size="sm" />
            )}
            <Button variant="outline" size="sm" onClick={goEvents}>
              <ExternalLink className="size-3.5" />
              View events
            </Button>
          </>
        }
      />
      {isError && (
        <p role="alert" className="text-body-sm text-destructive">
          Couldn't refresh this event type: {getErrorMessage(error)}
        </p>
      )}

      {/* The shared Radix tabs, URL-controlled; a switch goes
          through the page guard. Manual activation: with selection on focus,
          the focus the guard's dialog hands back on "Keep editing" would
          select the tab again and re-open the dialog. */}
      <Tabs
        value={tab}
        onValueChange={(next) => setTab(next as DetailTab)}
        activationMode="manual"
        className="gap-[18px]"
      >
        <TabsList aria-label="Event type sections">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="events">
          {/* The real, filterable events table embedded inline and scoped to
              this type (lockType decouples it from the URL :tab segment;
              embedded hides the page header + aggregate chart). */}
          <EventsPage lockType={et.name} embedded />
        </TabsContent>
        <TabsContent value="summary">
          <SummaryTab et={et} />
          {/* Health (F15, #268): main-plan only, nothing on a branch. */}
          <EventTypeHealthSummary
            className="mt-4"
            slug={slug}
            eventTypeId={et.id}
            onMain={branchId === null}
          />
          {/* What depends on this type: its events, the metrics and alert
              rules built on it, the relations that link its fields (#257). */}
          <UsedBySection
            className="mt-4"
            slug={slug}
            entity={{ kind: 'event_type', id: et.id }}
            branchId={branchId}
          />
          {/* Docs-catalog notes linking to this type or its fields (F22).
              Links resolve by name against main: main only. */}
          {branchId === null && (
            <>
              <DocNotesSection className="mt-4" slug={slug} kind="event_type" name={et.name} />
              <DocFieldNotes
                className="mt-3"
                slug={slug}
                eventTypeName={et.name}
                fieldNames={[...et.field_definitions].sort((a, b) => a.order - b.order).map((f) => f.name)}
              />
            </>
          )}
        </TabsContent>
        <TabsContent value="settings">
          <SettingsTab slug={slug} eventType={et} branchId={branchId} onDeleted={goBack} />
        </TabsContent>
      </Tabs>
    </PageContainer>
  )
}

// ─────────────────────────── Summary tab ───────────────────────────

function SummaryTab({ et }: { et: EventType }) {
  const sensitive = sensitiveFieldCount(et)
  const required = requiredFieldCount(et)
  const stats: { label: string; value: ReactNode; warning?: boolean }[] = [
    { label: 'Fields', value: et.field_definitions.length },
    { label: 'Required fields', value: required },
    { label: 'Sensitive fields', value: sensitive, warning: sensitive > 0 },
    { label: 'Enum fields', value: et.field_definitions.filter((f) => f.field_type === 'enum').length },
  ]

  const sensitiveFields = et.field_definitions
    .filter((f) => f.sensitivity !== 'none')
    .sort((a, b) => a.order - b.order)
    .slice(0, 7)

  return (
    <div className="flex flex-col gap-4">
      {/* The one KPI strip, counts in sans + tabular digits. */}
      <MiniStatStrip boxed>
        {stats.map((s) => (
          <MiniStat
            key={s.label}
            label={s.label}
            value={s.value}
            valueTone={s.warning ? 'warning' : undefined}
          />
        ))}
      </MiniStatStrip>

      <div className="grid items-start gap-3 md:grid-cols-2">
        <Panel title="About">
          <div className="flex flex-col gap-2.5 px-4 py-3">
            <KeyValue label="Name" value={<span className="mono">{et.name}</span>} />
            <KeyValue label="Description" value={et.description || '—'} />
            {/* The field list itself, not the counts again: the strip above
                already has them. */}
            <KeyValue
              label="Fields"
              value={
                et.field_definitions.length === 0 ? (
                  '—'
                ) : (
                  <span className="mono break-words">
                    {[...et.field_definitions]
                      .sort((a, b) => a.order - b.order)
                      .map((f) => f.name)
                      .join(', ')}
                  </span>
                )
              }
            />
          </div>
        </Panel>

        <Panel title="Sensitive fields" subtitle="Fields carrying a sensitivity label">
          {sensitiveFields.length === 0 ? (
            <p className="px-4 py-6 text-center text-body-sm text-fg-tertiary">
              No sensitive fields.
            </p>
          ) : (
            <div className="py-1">
              {sensitiveFields.map((f) => (
                <div
                  key={f.id}
                  className="flex items-center gap-2.5 border-t px-4 py-1.5 border-border-subtle"
                >
                  <span className="mono flex-1 truncate text-caption">{f.name}</span>
                  <Chip tone="warning" size="xs">
                    {f.sensitivity}
                  </Chip>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}

function KeyValue({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex gap-3.5 text-body-sm">
      <span className="w-[90px] shrink-0 text-fg-tertiary">
        {label}
      </span>
      <span className="flex-1 leading-snug text-fg">
        {value}
      </span>
    </div>
  )
}

// ─────────────────────────── Settings tab ───────────────────────────

interface SettingsTabProps {
  slug: string
  eventType: EventType
  branchId: string | null
  onDeleted: () => void
}

function SettingsTab({ slug, eventType, branchId, onDeleted }: SettingsTabProps) {
  const canWrite = useCanWriteProject()
  // While a field is being added or edited, the other cards step aside, so the
  // page holds one form with one Save instead of the field page wedged between
  // General's and the Danger zone. `hidden`, not unmounted: a General
  // draft typed before "Add field" is still there on the way back.
  const [editingField, setEditingField] = useState(false)
  const onEditingChange = useCallback((editing: boolean) => setEditingField(editing), [])
  return (
    <div className="max-w-[880px]">
      {!canWrite && <ReadOnlyNotice className="mb-3" />}
      <div hidden={editingField}>
        <GeneralCard slug={slug} eventType={eventType} branchId={branchId} canWrite={canWrite} />
      </div>
      <FieldsEditor
        slug={slug}
        eventType={eventType}
        branchId={branchId}
        onEditingChange={onEditingChange}
      />
      <div hidden={editingField}>
        {branchId === null && <OwnersEditor slug={slug} eventType={eventType} />}
        {canWrite && (
          <DangerZoneCard slug={slug} eventType={eventType} branchId={branchId} onDeleted={onDeleted} />
        )}
      </div>
    </div>
  )
}

function GeneralCard({
  slug,
  eventType,
  branchId,
  canWrite,
}: {
  slug: string
  eventType: EventType
  branchId: string | null
  canWrite: boolean
}) {
  const qc = useQueryClient()
  const savedColor = eventType.color || DEFAULT_ENTITY_COLOR
  const [displayName, setDisplayName] = useState(eventType.display_name)
  const [description, setDescription] = useState(eventType.description)
  const [color, setColor] = useState(savedColor)
  // Save was always enabled and a save left no trace, so there was no telling
  // a saved card from an edited one.
  const dirty =
    displayName !== eventType.display_name
    || description !== eventType.description
    || color !== savedColor

  const updateMut = useMutation({
    // Its error is rendered under the card.
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      eventTypesApi.update(
        slug,
        eventType.id,
        { display_name: displayName, description, color },
        branchId,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) }),
  })

  // A viewer reads the definition; a disabled form kept live borders, a
  // colour picker and editing hints on controls that did nothing (#237 rule 4).
  if (!canWrite) {
    return (
      <Panel className="mb-3" title="General">
        <div className="p-4">
          <ReadOnlyDefinition
            items={[
              { label: 'Type name', value: <span className="mono">{eventType.name}</span> },
              { label: 'Display name', value: eventType.display_name },
              { label: 'Description', value: eventType.description },
              {
                label: 'Color',
                value: (
                  <span className="inline-flex items-center gap-1.5">
                    <span
                      aria-hidden="true"
                      className="inline-block size-3 rounded-full border"
                      style={{ background: savedColor, borderColor: 'var(--border-subtle)' }}
                    />
                    <span className="mono">{savedColor}</span>
                  </span>
                ),
              },
            ]}
          />
        </div>
      </Panel>
    )
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) updateMut.mutate()
      }}
    >
      <Panel
        className="mb-3"
        title="General"
        footer={
          <SaveFooter
            pending={updateMut.isPending}
            disabled={!dirty}
            // Says why Save is off, so the disabled button is not read
            // as a stray text link.
            status={!dirty ? (updateMut.isSuccess ? 'Saved' : 'No changes') : undefined}
          />
        }
      >
        {/* "Type name", not "Name": the field subpage below renders on the same
            screen with its own Name input, and two controls sharing one
            accessible name cannot be told apart by a screen reader. */}
        <SField label="Type name" hint="Used in queries and ingestion — can't be changed.">
          <SInput value={eventType.name} onChange={() => undefined} mono disabled />
        </SField>
        <SField label="Display name">
          <SInput value={displayName} onChange={setDisplayName} />
        </SField>
        <SField label="Description">
          <STextarea value={description} onChange={setDescription} />
        </SField>
        <SField label="Color" last>
          <ColorPicker value={color} onChange={setColor} />
        </SField>
      </Panel>
      {updateMut.isError && (
        <p role="alert" className="mb-3 text-body text-danger">
          {getErrorMessage(updateMut.error)}
        </p>
      )}
    </form>
  )
}

function DangerZoneCard({
  slug,
  eventType,
  branchId,
  onDeleted,
}: {
  slug: string
  eventType: EventType
  branchId: string | null
  onDeleted: () => void
}) {
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()

  const deleteMut = useMutation({
    mutationFn: () => eventTypesApi.del(slug, eventType.id, branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) })
      // The sidebar's event-type count reads the project summary.
      qc.invalidateQueries({ queryKey: projectKey(slug) })
      onDeleted()
    },
  })

  // Every status, deliberately. An unqualified events list excludes archived
  // events, so the count would omit exactly the rows the cascade still takes —
  // and under-counting in a delete confirm is worse than not counting at all.
  const impactQuery = useQuery({
    queryKey: eventTypeDeletionImpactKey(slug, branchId, eventType.id),
    queryFn: () =>
      eventsApi.list(
        slug,
        { event_type_id: eventType.id, status: EVENT_STATUSES, limit: 1 },
        branchId,
      ),
    // The failure is said in the card, in the delete's own words.
    meta: SILENT_ERROR_META,
  })
  // Delete waits for the count. The text used to read `total ?? 0` while the
  // count was pending or had failed, so a quick delete — or any delete after a
  // failed count — was confirmed with "nothing else is affected" over a cascade
  // taking every event of the type.
  const impact = impactQuery.isSuccess
    ? describeEventTypeDeletionImpact(
        impactQuery.data.total,
        eventType.field_definitions.length,
      )
    : impactQuery.isError
      ? 'Could not count the events that use this type. Deleting it deletes every one of '
        + 'them, including archived ones, and everything pointing at them: metrics composed '
        + 'from them stop producing values, alert rules filtered on them lose those filters, '
        + 'and their tuned sensitivity and chart markers are dropped. This cannot be undone.'
      : 'Counting the events that use this type…'
  // An unknown count still lets an owner delete, but only after reading the
  // worst case above; a pending one does not.
  const canDelete = impactQuery.isSuccess || impactQuery.isError

  const handleDelete = async () => {
    const ok = await confirm({
      title: 'Delete event type',
      message: (
        <ConfirmImpactMessage
          message={`Delete "${eventType.display_name}"? ${impact}`}
          slug={slug}
          branchId={branchId}
          mode="cascades"
          changes={[{ kind: 'event_type', id: eventType.id, change: 'delete' }]}
        />
      ),
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate()
  }

  return (
    <Panel className="mb-3" title="Danger zone" tone="danger">
      {dialog}
      <div className="flex items-center gap-[18px] px-4 py-3.5">
        <div className="flex-1">
          <div className="text-body font-medium">Delete event type</div>
          <div className="mt-0.5 text-body-sm text-fg-tertiary">
            {impact}
          </div>
        </div>
        {/* Bare red in a row; the solid red is the confirm dialog's. */}
        <Button
          variant="danger"
          size="sm"
          disabled={!canDelete || deleteMut.isPending}
          onClick={handleDelete}
        >
          <Trash2 className="size-3" />
          Delete
        </Button>
      </div>
    </Panel>
  )
}

// Real "gates merge" status: a type with owners gates branch merges; without
// owners, anyone can merge. Surfaced as a chip next to the title.
function MergeGateChip({ slug, eventType }: { slug: string; eventType: EventType }) {
  // Owners live on main under main's type id; a branch copy has neither, so
  // the request 404s and the chip would then claim "no owners".
  const branchId = useActiveBranchId()
  const { data: owners = [] } = useQuery({
    queryKey: eventTypeOwnersKey(slug, eventType.id),
    queryFn: () => eventTypeOwnersApi.list(slug, eventType.id),
    enabled: branchId === null,
  })
  if (branchId !== null) return null
  const gated = (owners as EventTypeOwner[]).length > 0
  return (
    <Chip
      tone={gated ? 'success' : 'warning'}
      size="sm"
      title={
        gated
          ? "Has owners — a branch that edits this type needs an owner's approval to merge"
          : 'No owners — anyone can merge changes to this type'
      }
    >
      {/* Who must approve a merge, in words. */}
      {gated ? 'Owner approval' : 'Open to merge'}
    </Chip>
  )
}
