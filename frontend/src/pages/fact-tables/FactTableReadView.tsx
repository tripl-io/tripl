import { ChevronLeft } from 'lucide-react'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { CodeToken } from '@/components/primitives/code-token'
import { SCard } from '@/components/settings/kit'
import { UsedBySection } from '@/components/dependencies/UsedBySection'
import { ReadOnlyDefinition, ReadOnlyNotice, type DefinitionItem } from '@/components/states'
import type { DataSource, FactTable } from '@/types'

/**
 * A fact table for someone who cannot edit it (#237). The editor used to
 * render for viewers inside a disabled fieldset: live borders, required stars,
 * "Run Preview to list the columns…" hints and a Preview button in the tab
 * order, and 2,000px of form chrome to scroll for a definition. Fact tables
 * have no detail page of their own, so this is their read view: the notice
 * once, then the definition as a description list, titled after the table.
 */
export function FactTableReadView({
  slug,
  factTable,
  dataSources,
  onClose,
}: {
  /** Enables the "Used by" section (#257); the read view renders without it. */
  slug?: string
  factTable: FactTable
  dataSources: readonly DataSource[]
  onClose: () => void
}) {
  const source = dataSources.find(ds => ds.id === factTable.data_source_id)
  const items: DefinitionItem[] = [
    { label: 'Internal name', value: <CodeToken>{factTable.name}</CodeToken> },
    { label: 'Description', value: factTable.description },
    { label: 'Data source', value: source?.name ?? null },
    {
      label: 'Timestamp column',
      value: factTable.timestamp_column ? <CodeToken>{factTable.timestamp_column}</CodeToken> : null,
    },
    {
      label: 'Identifier columns',
      value:
        factTable.identifier_columns.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {factTable.identifier_columns.map(column => (
              <CodeToken key={column}>{column}</CodeToken>
            ))}
          </span>
        ) : null,
    },
    {
      label: 'SQL',
      block: true,
      value: (
        <pre
          aria-label="Fact table SQL"
          className="m-0 max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg-sunken px-3 py-2 font-mono text-caption text-fg"
        >
          {factTable.sql}
        </pre>
      ),
    },
    {
      label: 'Columns',
      block: true,
      value:
        factTable.columns.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {factTable.columns.map(column => (
              <CodeToken key={column.name} title={column.type}>
                {column.name}
              </CodeToken>
            ))}
          </span>
        ) : null,
    },
    {
      label: 'Row filters',
      block: true,
      value:
        factTable.row_filters.length > 0 ? (
          <ul className="m-0 list-none space-y-1.5 p-0">
            {factTable.row_filters.map(filter => (
              <li key={filter.name} className="min-w-0">
                <span className="font-medium">{filter.name}</span>{' '}
                <CodeToken>{filter.sql}</CodeToken>
              </li>
            ))}
          </ul>
        ) : null,
    },
  ]

  return (
    <div className="h-full overflow-y-auto">
      <PageContainer width="narrow">
        <button
          type="button"
          onClick={onClose}
          className="mb-[14px] inline-flex items-center gap-1 text-caption text-fg-muted transition-colors hover:text-fg"
        >
          <ChevronLeft className="size-3.5" aria-hidden="true" /> Fact tables
        </button>
        <PageHeader
          className="mb-[18px]"
          eyebrow="Observe · Fact table"
          title={factTable.display_name || factTable.name}
        />
        <ReadOnlyNotice className="mb-[18px]" />
        <SCard title="Fact table definition">
          <div className="px-4 py-[14px]">
            <ReadOnlyDefinition items={items} />
          </div>
        </SCard>
        {slug && (
          <UsedBySection
            className="mt-[18px]"
            slug={slug}
            entity={{ kind: 'fact_table', id: factTable.id }}
            branchId={null}
          />
        )}
      </PageContainer>
    </div>
  )
}
