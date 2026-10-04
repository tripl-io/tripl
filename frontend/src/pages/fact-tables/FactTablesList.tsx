import { formatNumber } from '@/lib/format'
import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, MoreVertical, Pencil, Plus, Sheet, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { dataSourcesApi } from '@/api/dataSources'
import { factTablesApi } from '@/api/factTables'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useConfirm } from '@/hooks/useConfirm'
import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { SectionSkeleton, StatValueSkeleton } from '@/components/states'
import { Panel } from '@/components/settings/kit'
import { Chip } from '@/components/primitives/chip'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { Skeleton } from '@/components/ui/skeleton'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { formatRelativeTime } from '@/lib/datetime'
import type { FactTableListItem } from '@/types'
import { countOf } from '@/lib/plural'
import { dataSourcesKey, factTablesKey, projectFactTableKey } from '@/lib/queryKeys'
import { useCanWriteProject } from '@/lib/permissions'
import { buildFactTableCopy } from './factTableCopy'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const FACT_TABLE_GRID =
  'grid grid-cols-[1.7fr_1fr_1fr_96px_84px_24px] items-center gap-3 px-4'
  // Below md a row is a two-line card, the catalog's pattern, instead of a
  // 680px strip a phone scrolls sideways: name and updated on top, the
  // data source under the name; the timestamp column is what a phone drops.
  + ' max-md:grid-cols-[minmax(0,1fr)_auto] max-md:gap-y-1'
/** Where each cell sits in the phone card; no effect from md up. */
const PHONE_CELL = {
  name: 'max-md:col-start-1 max-md:row-start-1',
  source: 'max-md:col-start-1 max-md:row-start-2',
  timestamp: 'max-md:hidden',
  usedBy: 'max-md:hidden',
  updated: 'max-md:col-start-2 max-md:row-start-1',
  actions: 'max-md:col-start-2 max-md:row-start-2 max-md:justify-self-end',
} as const

/**
 * Fact tables list body — the stat rollup and table rows. Rendered as the
 * "Fact tables" tab inside {@link MetricsPage}; owns its own data fetching and
 * spacing but not the page header/tab chrome (the parent provides those). Fact
 * tables exist only to back fact metrics, so they live under Metrics rather
 * than as a standalone surface.
 */
export function FactTablesList({ slug }: { slug?: string }) {
  const canWrite = useCanWriteProject()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { confirm, dialog: confirmDialog } = useConfirm()
  // Delete from the row, as the editor does. The API refuses a table metrics
  // still read and names them; that refusal renders inside the dialog.
  const deleteTable = (table: FactTableListItem) => {
    if (!slug) return
    void confirm({
      title: 'Delete this fact table?',
      // The metrics that read it, listed before the attempt (#257). These are
      // the one dependents that DO block: the server refuses with a 409.
      message: (
        <ConfirmImpactMessage
          message={
            `"${table.display_name}" disappears from every fact metric's picker. A fact table ` +
            'that metrics still read cannot be deleted; the refusal names them.'
          }
          slug={slug}
          branchId={null}
          mode="blocks"
          changes={[{ kind: 'fact_table', id: table.id, change: 'delete' }]}
        />
      ),
      confirmLabel: 'Delete fact table',
      variant: 'danger',
      errorPrefix: 'Could not delete the fact table',
      pendingLabel: 'Deleting…',
      action: async () => {
        await factTablesApi.remove(slug, table.id)
        void qc.invalidateQueries({ queryKey: factTablesKey(slug) })
        void qc.invalidateQueries({ queryKey: projectFactTableKey(slug) })
        toast.success('Fact table deleted.')
      },
    })
  }
  const factTablesQuery = useQuery({
    queryKey: factTablesKey(slug),
    queryFn: () => factTablesApi.list(slug!),
    enabled: !!slug,
    staleTime: 30_000,
  })

  // Resolve data-source ids to names for the table column. The list endpoint
  // returns ids only; data sources are workspace-scoped and small, so a single
  // cached query is cheaper than denormalising names server-side.
  const dataSourcesQuery = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: () => dataSourcesApi.list(),
    // Said inline above the table, with a retry.
    meta: SILENT_ERROR_META,
  })
  const dataSourceNamesState: DataSourceNamesState = dataSourcesQuery.isError
    ? 'error'
    : dataSourcesQuery.isPending
      ? 'loading'
      : 'ready'

  const data = factTablesQuery.data
  const factTables = useMemo(() => data?.items ?? [], [data])
  const dataSourceNames = useMemo(() => {
    const map = new Map<string, string>()
    for (const ds of dataSourcesQuery.data ?? []) map.set(ds.id, ds.name)
    return map
  }, [dataSourcesQuery.data])

  // Duplicate from the row (F7): the list row carries no SQL, so the full
  // table is read first, then created under a free `_copy` name, and the copy
  // opens in the editor to be renamed or changed.
  const duplicateMut = useMutation({
    mutationFn: async (table: FactTableListItem) => {
      const source = await factTablesApi.get(slug!, table.id)
      const names = new Set(factTables.map(t => t.name))
      return factTablesApi.create(slug!, buildFactTableCopy(source, names))
    },
    onSuccess: created => {
      void qc.invalidateQueries({ queryKey: factTablesKey(slug) })
      toast.success('Fact table duplicated.')
      navigate(projectPath(currentOrgSlug(), slug, `/metrics/fact-tables/${created.id}/edit`))
    },
  })

  // Loaded with no fact tables — the true "nothing here yet" state, distinct
  // from loading and error.
  const isEmpty = !factTablesQuery.isError && !!data && factTables.length === 0

  return (
    <div className={isEmpty ? 'flex min-h-[calc(100vh-14rem)] flex-col gap-6' : 'space-y-6'}>
      {factTablesQuery.isError ? (
        <ErrorState
          title="Fact tables unavailable"
          error={factTablesQuery.error}
          onRetry={() => {
            void factTablesQuery.refetch()
          }}
          retryLabel="Retry"
          compact
        />
      ) : (
        <MiniStatStrip boxed className={isEmpty ? 'opacity-60' : undefined}>
          <MiniStat
            label="Fact tables"
            value={data ? formatNumber(data.total ?? factTables.length) : <StatValueSkeleton />}
          />
          {/* Counts the sources fact tables read, not every connected one: the
              label says so, so an empty project's 0 beside two connected
              warehouses is not a contradiction. */}
          <MiniStat
            label="Sources in use"
            value={
              data ? (
                formatNumber(new Set(factTables.map(t => t.data_source_id).filter(Boolean)).size)
              ) : (
                <StatValueSkeleton />
              )
            }
          />
          {/* Tables at least one metric reads. Not a sum of
              `metric_count`: that counts ratio operands too, so a cross-table
              ratio would be counted once per table it reads. */}
          <MiniStat
            label="Tables in use"
            value={
              data ? (
                formatNumber(factTables.filter(t => (t.metric_count ?? 0) > 0).length)
              ) : (
                <StatValueSkeleton />
              )
            }
          />
        </MiniStatStrip>
      )}

      {!factTablesQuery.isError &&
        (isEmpty ? (
          <div className="flex flex-1 items-center justify-center">
            <EmptyState
              icon={Sheet}
              title="No fact tables yet"
              description="A fact table is a saved query over your warehouse, for example one row per order. Fact metrics then sum, count or average its columns without writing SQL."
              action={
                slug && canWrite ? (
                  <Button asChild size="sm">
                    <Link to={projectPath(currentOrgSlug(), slug, '/metrics/fact-tables/new')} className="no-underline">
                      <Plus className="h-3.5 w-3.5" />
                      New fact table
                    </Link>
                  </Button>
                ) : undefined
              }
            />
          </div>
        ) : (
          /* "Fact tables", not "Catalog". This panel carried the same
             hardcoded title as the panel on the *other* tab
             (metrics/MetricsCatalog.tsx), so on the Fact tables tab the only
             panel on the page was named after the tab you are not on — its
             header sat 154px below the unselected "Catalog" tab, at the same
             spot the Catalog tab's own panel header occupies, so a reader
             glancing at it to work out where they are got the wrong answer.

             Renaming it took the "N total" subtitle with it, on the grounds
             that the stat strip 60px above already states the same number.
             True — and equally true of the Catalog tab, which keeps
             "Catalog / 4 total" under a "METRICS 4" stat. Dropping it here
             only made the two tabs of one page disagree about whether a list
             panel is captioned, so the two panel headers no longer lined up.
             Same expression as MetricsCatalog.tsx:603 so the
             sibling headers stay one shape. */
          <Panel
            title="Fact tables"
            subtitle={data ? `${formatNumber(data.total ?? factTables.length)} total` : undefined}
          >
            {factTablesQuery.isLoading ? (
              // Rows in the table's shape, not one grey word (#237).
              <SectionSkeleton variant="rows" label="Loading fact tables…" />
            ) : (
              <div className="overflow-x-auto">
                {dataSourceNamesState === 'error' && (
                  <div
                    role="status"
                    className="flex flex-wrap items-center gap-2 border-b px-4 py-2 text-body-sm border-border-subtle text-warning"
                  >
                    Data source names could not be loaded.
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 px-2 text-caption"
                      onClick={() => {
                        void dataSourcesQuery.refetch()
                      }}
                    >
                      Retry
                    </Button>
                  </div>
                )}
                <div role="table" aria-label="Fact tables" className="md:min-w-[760px]">
                  <div role="rowgroup">
                    <div
                      role="row"
                      className={`${FACT_TABLE_GRID} border-b py-2 micro-label border-border-subtle text-fg-tertiary`}
                    >
                      <span role="columnheader" className={PHONE_CELL.name}>Fact table</span>
                      {/* The card shows the source under the name, unlabelled;
                          screen readers still get the header. */}
                      <span role="columnheader" className="max-md:sr-only">Data source</span>
                      <span role="columnheader" className={PHONE_CELL.timestamp}>Timestamp column</span>
                      <span role="columnheader" className={PHONE_CELL.usedBy}>Used by</span>
                      <span role="columnheader" className={`text-right ${PHONE_CELL.updated}`}>Updated</span>
                      <span role="columnheader" className="sr-only">Actions</span>
                    </div>
                  </div>
                  <div role="rowgroup">
                    {factTables.map(table => (
                      <FactTableRow
                        key={table.id}
                        table={table}
                        slug={slug}
                        dataSourceName={
                          table.data_source_id
                            ? dataSourceNames.get(table.data_source_id) ?? null
                            : null
                        }
                        dataSourceNamesState={dataSourceNamesState}
                        onDelete={canWrite ? () => deleteTable(table) : undefined}
                        onDuplicate={
                          canWrite && !duplicateMut.isPending
                            ? () => duplicateMut.mutate(table)
                            : undefined
                        }
                      />
                    ))}
                  </div>
                </div>
              </div>
            )}
          </Panel>
        ))}
      {confirmDialog}
    </div>
  )
}

/** Where the id → name lookup for the Data source column stands. */
type DataSourceNamesState = 'loading' | 'error' | 'ready'

interface FactTableRowProps {
  table: FactTableListItem
  slug?: string
  dataSourceName: string | null
  dataSourceNamesState: DataSourceNamesState
  /** Offered to writers only; readers get no row menu. */
  onDelete?: () => void
  /** Offered to writers only, and not while another copy is being made. */
  onDuplicate?: () => void
}

/**
 * The Data source cell. It used to print "—" for four different things — no
 * source, names still loading, names failed to load, and a source that was
 * deleted — so a broken fact table looked like a slow fetch.
 */
function DataSourceCell({
  hasSource,
  name,
  state,
}: {
  hasSource: boolean
  name: string | null
  state: DataSourceNamesState
}) {
  // The FK is ON DELETE SET NULL and the editor requires a source, so a null
  // source means its data source was deleted: that is the main MET-37 case.
  if (!hasSource) {
    return (
      <span title="This fact table has no data source; the one it read was deleted. Pick another in the editor.">
        <Chip tone="warning" size="xs">
          Missing source
        </Chip>
      </span>
    )
  }
  if (name) return <>{name}</>
  if (state === 'loading') {
    return (
      <>
        <Skeleton className="h-3 w-24" />
        <span className="sr-only">Loading data source</span>
      </>
    )
  }
  if (state === 'error') return <span className="text-fg-tertiary">Unavailable</span>
  return (
    <span title="The data source this fact table reads was deleted. Pick another in the editor.">
      <Chip tone="warning" size="xs">
        Missing source
      </Chip>
    </span>
  )
}

function FactTableRow({
  table,
  slug,
  dataSourceName,
  dataSourceNamesState,
  onDelete,
  onDuplicate,
}: FactTableRowProps) {
  const navigate = useNavigate()
  const href = slug ? projectPath(currentOrgSlug(), slug, `/metrics/fact-tables/${table.id}/edit`) : undefined
  const usedByTitle = `${countOf(table.column_count ?? 0, 'column', 'columns')}, ${countOf(table.identifier_count ?? 0, 'identifier', 'identifiers')}`

  // The whole row opens the table, as a catalog row does; the name
  // Link stays the keyboard route, so the row adds no Tab stop of its own.
  return (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events -- pointer-only convenience; the name Link is the keyboard route
    <div
      role="row"
      // `--row-h` floor: the Appearance density reaches this list too.
      className={`${FACT_TABLE_GRID} min-h-(--row-h) border-b py-1.5 last:border-0 ${
        href ? 'cursor-pointer transition-colors hover:bg-[var(--surface-hover)]' : ''
      } border-border-subtle`}
      onClick={href ? () => navigate(href) : undefined}
    >
      <span role="cell" className={`flex min-w-0 items-center gap-2 ${PHONE_CELL.name}`}>
        <span
          className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
          style={{ background: table.color }}
        />
        {href ? (
          <Link
            to={href}
            onClick={event => event.stopPropagation()}
            className="truncate text-body-sm font-medium no-underline hover:underline text-fg"
          >
            {table.display_name}
          </Link>
        ) : (
          <span className="truncate text-body-sm font-medium">{table.display_name}</span>
        )}
        <span className="mono truncate text-caption text-fg-tertiary">
          {table.name}
        </span>
      </span>
      <span role="cell" className={`truncate text-body-sm ${PHONE_CELL.source} text-fg-tertiary`}>
        <DataSourceCell
          hasSource={!!table.data_source_id}
          name={dataSourceName}
          state={dataSourceNamesState}
        />
      </span>
      <span role="cell" className={`mono truncate text-body-sm ${PHONE_CELL.timestamp} text-fg-tertiary`}>
        {table.timestamp_column || <span className="text-fg-tertiary">—</span>}
      </span>
      {/* The column count rides in the title: a sixth column would push the
          row past a laptop's width. A count opens the catalog
          narrowed to the metrics that read this table (F7). */}
      <span role="cell" className={`tnum truncate text-body-sm ${PHONE_CELL.usedBy}`}>
        {table.metric_count && slug ? (
          <Link
            to={projectPath(currentOrgSlug(), slug, `/metrics?fact_table=${encodeURIComponent(table.id)}`)}
            onClick={event => event.stopPropagation()}
            className="underline-offset-2 hover:underline text-fg-tertiary"
            title={usedByTitle}
          >
            {countOf(table.metric_count, 'metric', 'metrics')}
          </Link>
        ) : (
          <span style={{ color: table.metric_count ? 'var(--fg-subtle)' : 'var(--fg-faint)' }} title={usedByTitle}>
            {table.metric_count ? countOf(table.metric_count, 'metric', 'metrics') : 'No metrics'}
          </span>
        )}
      </span>
      <span role="cell" className={`tnum text-right text-micro ${PHONE_CELL.updated} text-fg-tertiary`}>
        {formatRelativeTime(table.updated_at)}
      </span>
      <span role="cell" className={`flex justify-end ${PHONE_CELL.actions}`}>
        {href && onDelete && (
          <FactTableRowMenu
            name={table.display_name}
            href={href}
            onDelete={onDelete}
            onDuplicate={onDuplicate}
          />
        )}
      </span>
    </div>
  )
}

/**
 * Edit, Duplicate and Delete from the row, the catalog's row-menu pattern.
 * Duplicate fetches the table's full body first: the list row carries no SQL.
 */
function FactTableRowMenu({
  name,
  href,
  onDelete,
  onDuplicate,
}: {
  name: string
  href: string
  onDelete: () => void
  /** Absent while a copy is being made. */
  onDuplicate?: () => void
}) {
  const navigate = useNavigate()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Actions for ${name}`}
          className="flex items-center justify-center rounded-sm p-0.5 hover:bg-[var(--surface-hover)] text-fg-tertiary"
          onClick={event => event.stopPropagation()}
        >
          <MoreVertical className="h-3.5 w-3.5" />
        </button>
      </DropdownMenuTrigger>
      {/* Portaled clicks still bubble through the React tree to the row's
          navigate-on-click. */}
      <DropdownMenuContent
        align="end"
        sideOffset={6}
        className="w-[184px]"
        onClick={event => event.stopPropagation()}
      >
        <DropdownMenuItem className="text-body-sm" onSelect={() => navigate(href)}>
          <Pencil className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Edit
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-body-sm"
          disabled={!onDuplicate}
          onSelect={() => onDuplicate?.()}
        >
          <Copy className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Duplicate
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem className="text-body-sm" variant="destructive" onSelect={onDelete}>
          <Trash2 className="h-3.5 w-3.5 shrink-0" /> Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
