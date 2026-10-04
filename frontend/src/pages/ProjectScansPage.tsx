import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { ScansTab } from './settings/ScansTab'
import { ScanConfigDetail } from './settings/ScanConfigDetailView'
import { ScanCreatePage } from './settings/scans/ScanConfigForm'
import { useIsOwner } from '@/lib/permissions'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/** A scan id is a UUID, so `new` under `/p/:slug/scans/:scanId` is never one. */
const NEW_SCAN_SEGMENT = 'new'

/**
 * The create page has a route: it used to be view state inside the
 * list, so Back left Scans altogether and a reload dropped the whole draft.
 * Authoring a scan is owner-only; anyone else is sent to the list.
 */
function NewScanRoute({ slug }: { slug: string }) {
  const navigate = useNavigate()
  const isOwner = useIsOwner()
  if (!isOwner) return <Navigate to={projectPath(currentOrgSlug(), slug, '/scans')} replace />
  return (
    <ScanCreatePage
      slug={slug}
      onBack={() => navigate(projectPath(currentOrgSlug(), slug, '/scans'))}
      // Replace the /new entry: Back from the created scan goes to the list,
      // not to an empty New scan form.
      onCreated={created => navigate(projectPath(currentOrgSlug(), slug, `/scans/${created.id}`), { replace: true })}
    />
  )
}

/**
 * Govern › Scans, as a top-level project surface at `/p/:slug/scans`.
 *
 * Scans are an operational surface — you run them, watch them, and read what
 * they changed — not project configuration, so they no longer live under
 * `/settings`. This page is a thin dispatcher over the two components that
 * already existed as `ProjectSettingsPage` tab branches; both keep their
 * current props and import paths.
 *
 * It deliberately renders NO settings signpost. The old mount inherited
 * `ProjectSettingsPage`'s chrome, which framed the page as "Project
 * operations" while linking out to "Workspace settings" — two different
 * claims about what surface you were on, above a page that is neither.
 */
export default function ProjectScansPage() {
  const { slug, scanId } = useParams<{ slug: string; scanId?: string }>()
  if (!slug) return null
  if (scanId === NEW_SCAN_SEGMENT) return <NewScanRoute slug={slug} />
  return scanId
    ? <ScanConfigDetail slug={slug} scanConfigId={scanId} />
    : <ScansTab slug={slug} />
}
