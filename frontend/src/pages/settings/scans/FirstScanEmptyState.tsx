import { Link } from 'react-router-dom'
import { settingsPath } from '@/lib/activeOrg'
import { Database, Plus, ScanSearch } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { SectionSkeleton } from '@/components/states'

/** What setting up a scan involves, in the order the product asks for it. */
const SETUP_STEPS = [
  'Add a connection to your warehouse',
  'Write the query the scan reads',
  'Preview what it would create, then create it',
] as const

function SetupSteps({ from }: { from: number }) {
  return (
    <ol className="space-y-1.5 text-left text-body-sm" aria-label="Setting up a scan">
      {SETUP_STEPS.map((step, index) => (
        <li
          key={step}
          className="flex items-baseline gap-2"
          style={{ color: index + 1 < from ? 'var(--fg-faint)' : 'var(--fg-muted)' }}
        >
          <span className="tnum w-3 shrink-0 text-right font-semibold text-fg">
            {index + 1}
          </span>
          {step}
        </li>
      ))}
    </ol>
  )
}

/**
 * The Scans page of a project with no scans: ONE empty state, instead of three
 * "0" tiles over "No data sources" over an "All scans · 0 scans" panel. It says
 * what setting up a scan involves and offers the step that comes next
 * (#247).
 */
export function FirstScanEmptyState({
  isOwner,
  dataSourcesSettled,
  noDataSources,
  dataSourcesError,
  onRetryDataSources,
  onNewScan,
}: {
  isOwner: boolean
  /** The data-source list answered (or failed), so "none" can be told from "not yet". */
  dataSourcesSettled: boolean
  noDataSources: boolean
  /**
   * The data-source request failed. Without the list there is no telling
   * whether step 1 is done, so the page says it could not check rather than
   * offering "Create your first scan" to a project that may have no connection.
   */
  dataSourcesError?: unknown
  onRetryDataSources?: () => void
  onNewScan: () => void
}) {
  if (!dataSourcesSettled) {
    return <SectionSkeleton variant="list" rows={2} label="Loading data sources…" />
  }

  if (dataSourcesError) {
    return (
      <ErrorState
        title="Could not load this project’s data sources"
        description="A scan starts with a warehouse connection, and the list of connections did not load."
        error={dataSourcesError}
        onRetry={onRetryDataSources}
      />
    )
  }

  if (noDataSources) {
    return (
      <EmptyState
        icon={Database}
        title="Connect your warehouse to start scanning"
        description={
          isOwner
            ? 'A scan reads a query from your warehouse into your tracking plan. It starts with a connection.'
            : 'A scan reads a query from your warehouse into your tracking plan. An owner has to add the warehouse connection first.'
        }
        action={
          <div className="flex flex-col items-center gap-4">
            <SetupSteps from={1} />
            {/* Data sources are owner-only: anyone else gets the steps and no
                button they cannot use. */}
            {isOwner && (
              <Button asChild size="lg">
                <Link to={settingsPath('/settings/data-sources')}>
                  <Plus className="size-4" aria-hidden="true" />
                  Add connection
                </Link>
              </Button>
            )}
          </div>
        }
      />
    )
  }

  return (
    <EmptyState
      icon={ScanSearch}
      title={isOwner ? 'Create your first scan' : 'No scans yet'}
      description={
        isOwner
          ? 'A scan reads a query from your warehouse into your tracking plan. You preview what it would create before you save it.'
          : 'An owner creates scans. Once one exists, it is listed here with its runs.'
      }
      action={
        isOwner ? (
          <div className="flex flex-col items-center gap-4">
            <SetupSteps from={2} />
            <Button size="lg" onClick={onNewScan}>
              <Plus className="size-4" aria-hidden="true" />
              New scan
            </Button>
          </div>
        ) : undefined
      }
    />
  )
}
