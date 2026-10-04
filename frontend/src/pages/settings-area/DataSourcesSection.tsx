import { Suspense } from 'react'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { SHeader } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'

const DataSourcesPage = lazyWithReload(() => import('@/pages/DataSourcesPage'))

/**
 * Workspace · Data sources. Reuses the existing DataSourcesPage wiring verbatim
 * (it reads dsId from the route and navigates back to /settings/data-sources),
 * wrapped in the takeover section header.
 */
export default function DataSourcesSection() {
  return (
    <div>
      <SHeader
        title="Data sources"
        description="Warehouse connections your scans read from. Each connection carries its own credentials and can be used by scans in any project."
      />
      <Suspense
        // The roster's shape under the header, not a 14px "Loading…" (#237).
        fallback={<SectionSkeleton variant="list" label="Loading data sources…" />}
      >
        <DataSourcesPage />
      </Suspense>
    </div>
  )
}
