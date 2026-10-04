import { Suspense } from 'react'
import { Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { PageSkeleton, type PageSkeletonVariant } from '@/components/states'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { currentOrgSlug, legacySettingsRedirectPath, projectPath, settingsPath, type ProjectSurfaceMovedFromSettings } from '@/lib/navigation'

// Each surface is its own chunk. They are separate sidebar destinations and
// only one renders at a time, but importing them statically put all nine —
// the branch diff UI, the audit table, the events table under the event-type
// detail — into one chunk that every one of them downloaded (#194).
const AuditTab = lazyWithReload(() =>
  import('./settings/AuditTab').then((m) => ({ default: m.AuditTab })),
)
const BranchesTab = lazyWithReload(() =>
  import('./settings/BranchesTab').then((m) => ({ default: m.BranchesTab })),
)
const EventTypesTab = lazyWithReload(() =>
  import('./settings/EventTypesTab').then((m) => ({ default: m.EventTypesTab })),
)
const EventTypeDetail = lazyWithReload(() =>
  import('./settings/EventTypeDetailView').then((m) => ({ default: m.EventTypeDetail })),
)
const HistoryTab = lazyWithReload(() =>
  import('./settings/HistoryTab').then((m) => ({ default: m.HistoryTab })),
)
const MetaFieldsTab = lazyWithReload(() =>
  import('./settings/MetaFieldsTab').then((m) => ({ default: m.MetaFieldsTab })),
)
const RelationsTab = lazyWithReload(() =>
  import('./settings/RelationsTab').then((m) => ({ default: m.RelationsTab })),
)
const VariablesTab = lazyWithReload(() =>
  import('./settings/VariablesTab').then((m) => ({ default: m.VariablesTab })),
)
const VariableDetailPage = lazyWithReload(() =>
  import('./settings/variable-detail/VariableDetailPage').then((m) => ({
    default: m.VariableDetailPage,
  })),
)
const MonitoringTab = lazyWithReload(() =>
  import('./settings/MonitoringTab').then((m) => ({ default: m.MonitoringTab })),
)
const ProjectAlertingTab = lazyWithReload(() => import('@/pages/ProjectAlertingTab'))

/**
 * A surface's chunk loading: the shape of the page it is about to become, not
 * a "Loading…" line in an empty column (#237). Each surface
 * renders its own header, so the skeleton draws one too.
 */
const TAB_SKELETON: Record<FunctionalTab, PageSkeletonVariant> = {
  'event-types': 'list',
  'meta-fields': 'list',
  relations: 'list',
  variables: 'list',
  monitoring: 'settings',
  alerting: 'list',
  branches: 'detail',
  history: 'list',
  audit: 'list',
}

function TabFallback({ tab, detail }: { tab: FunctionalTab; detail: boolean }) {
  return <PageSkeleton variant={detail ? 'detail' : TAB_SKELETON[tab]} />
}

/**
 * Functional project surfaces (event types, meta fields, variables, relations,
 * branches, history, alerting, audit) plus the detection settings.
 *
 * The Plan, Observe and Govern surfaces are first-class sidebar pages routed at
 * `/p/:slug/<surface>[/:itemId]` (#238); App.tsx mounts
 * this page for each with `surface` set. `/p/:slug/settings/<tab>` keeps only
 * project settings: `monitoring` (detection) renders here, `general` and
 * `plan-rules` go to the full-takeover Settings area, and every surface that
 * moved out redirects to its new address with the item id, query string and
 * hash intact — alert messages already sent carry `?item=` / `?incident=`.
 *
 * `scans` is deliberately absent: it moved to the top-level `/p/:slug/scans`
 * route, and the legacy `/p/:slug/settings/scans[/:itemId]` paths are claimed by
 * `ScansRedirect` in App.tsx — a more specific match than `/settings/:tab`, so
 * this component never sees the tab.
 */
type FunctionalTab =
  | 'event-types'
  | 'meta-fields'
  | 'relations'
  | 'variables'
  | 'monitoring'
  | 'alerting'
  | 'branches'
  | 'history'
  | 'audit'

// Every surface except detection is routed outside `/settings` — only
// `monitoring` is still reached through the `:tab` segment.
export default function ProjectSettingsPage({
  surface,
}: {
  surface?: ProjectSurfaceMovedFromSettings
}) {
  const { slug, tab: urlTab, itemId } = useParams<{ slug: string; tab?: string; itemId?: string }>()
  const location = useLocation()
  // `?item=<scope_type>:<scope_ref>` names ONE row inside the delivery `itemId`
  // points at. A delivery carries up to 8 items, so the path segment alone
  // identifies the page but not the line the alert message quoted.
  const [searchParams] = useSearchParams()
  const focusItemKey = searchParams.get('item') ?? undefined
  // `?scan=<scan_config_id>` narrows the alerting audit log to one scan — the
  // target of the "Alerts queued" counter on a scan run.
  const focusScanId = searchParams.get('scan') ?? undefined
  // The incident an alert link points at — the card that holds Ack / Resolve /
  // Mute. `item` still picks the row the message quoted inside it.
  const focusIncidentId = searchParams.get('incident') ?? undefined
  // `?focus=<id>` marks one row of the variables list: where the variable
  // page's back link returns to. `/p/:slug/variables/:id` is that variable's
  // own page now, so a branch diff's links — focus and Edit alike
  // — land on it, Definition tab first.
  const focusListId = searchParams.get('focus') ?? undefined

  if (!slug) return null

  let tab: FunctionalTab
  if (surface) {
    tab = surface
  } else {
    // Bare /p/:slug/settings and the old general config tab both belong to the
    // full-takeover Settings area now, as do the plan rules.
    if (!urlTab || urlTab === 'general') {
      return <Navigate to={settingsPath(`/settings/project/general?project=${encodeURIComponent(slug)}`)} replace />
    }
    if (urlTab === 'plan-rules') {
      return <Navigate to={settingsPath(`/settings/project/plan-rules?project=${encodeURIComponent(slug)}`)} replace />
    }
    if (urlTab === 'members') {
      return <Navigate to={settingsPath(`/settings/project/members?project=${encodeURIComponent(slug)}`)} replace />
    }
    const moved = legacySettingsRedirectPath(slug, urlTab, itemId, location.search, location.hash)
    if (moved) return <Navigate to={moved} replace />
    if (urlTab !== 'monitoring') return <Navigate to={projectPath(currentOrgSlug(), slug, '/events')} replace />
    tab = urlTab
  }

  return (
    // No "Project operations" signpost above the header any more (#238): these are Plan, Observe and Govern pages in the sidebar, and a
    // strip framing them as "settings" was the first thing above "Alerting".
    <div className="min-w-0">
      {/* Keyed by tab so moving between surfaces shows the fallback at once
          instead of leaving the previous surface up while the next loads. */}
      <Suspense key={tab} fallback={<TabFallback tab={tab} detail={!!itemId} />}>
        {tab === 'event-types' && itemId && <EventTypeDetail slug={slug} eventTypeId={itemId} />}
        {tab === 'event-types' && !itemId && <EventTypesTab slug={slug} />}
        {tab === 'meta-fields' && <MetaFieldsTab slug={slug} />}
        {tab === 'relations' && <RelationsTab slug={slug} />}
        {tab === 'variables' && itemId && <VariableDetailPage slug={slug} variableId={itemId} />}
        {tab === 'variables' && !itemId && <VariablesTab slug={slug} focusId={focusListId} />}
        {tab === 'monitoring' && <MonitoringTab slug={slug} />}
        {tab === 'alerting' && (
          <>
            {/* `itemId` focuses one delivery and `?item=` one row inside it —
                together the target of the deep link an alert message carries for
                scopes with no monitoring page. */}
            {/* Keyed by project: filters, drafts and an open destination dialog
                from project A must not carry into project B (a dialog would
                PATCH A's destination id under B's slug). */}
            <ProjectAlertingTab
              key={slug}
              slug={slug}
              focusDeliveryId={itemId}
              focusItemKey={focusItemKey}
              focusScanId={focusScanId}
              focusIncidentId={focusIncidentId}
            />
          </>
        )}
        {tab === 'branches' && <BranchesTab slug={slug} branchId={itemId} />}
        {tab === 'history' && <HistoryTab slug={slug} />}
        {tab === 'audit' && <AuditTab slug={slug} />}
      </Suspense>
    </div>
  )
}
